import os, re, sys, json, collections

import os
ROOT = os.environ.get("AGENT_AUDIO_ROOT", "")
AUDIO = (".wav", ".ogg", ".mp3", ".aif", ".aiff", ".flac", ".m4a")
SKIP_TOP = {"Audio_Assets"}  # never re-scan the output


# ---------------------------------------------------------------------------
# TASK 4 (2026-09-16): explicit re-categorisation of known misfiles.
# Applied AFTER the keyword rules; first substring match wins.
# A blanket keyword pass would make the taxonomy worse - most apparent
# anomalies are correct (rain-street is rain; subway tunnel is transit).
# ---------------------------------------------------------------------------
OVERRIDES = {
    'Big Rattling Truck At Tail (Neue Nationalgalerie)': ('Ambiance', 'City'),
    'ruck and police sirens, Tram, Crowd atmosphere, Turkish voices': ('Ambiance', 'City'),
    'City_36_Street, Light, Ambience, Cars, Horn': ('Ambiance', 'City'),
    'City_40_Street, Light, Ambience, Warning Alarm, Far': ('Ambiance', 'City'),
    'City_63_Street, Pedestrian, Ambience, Busy': ('Ambiance', 'City'),
    'Night_City_Big Street_Light Traffic_Hum': ('Ambiance', 'City'),
    'City_Street_Pavement_Single Car Passing By': ('Ambiance', 'City'),
    'Morning_Birds_Empty_Street_Distant_Traffic': ('Ambiance', 'City'),
    'City_Industrial_Street_Num_Buzz_Car Passing By': ('Ambiance', 'City'),
    'In Water Crowded Afternoon': ('Ambiance', 'Crowd'),
    'Tribal_25_Bells, Dance, People, Crowd,General, Ambience': ('Ambiance', 'Crowd'),
    'Tribal_05_Bells, Dance, People, Crowd, Ambience': ('Ambiance', 'Crowd'),
    'Water_10_City, Rain, Roof': ('Ambiance', 'City'),
    'CA1_Ambience_BerlinCity_AlexanderPlatz_Loop': ('Ambiance', 'City'),
    'City place quiet birds bell light activity and animals': ('Ambiance', 'City'),
    'Street Road Workers Clanging Dragging People Talking': ('Ambiance', 'City'),
    'Suburban Life - Children Playing Voices Shouts Kindergarten': ('Ambiance', 'Crowd'),
    'drum beat, excited crowd clapping in unison': ('Ambiance', 'Crowd'),
    'madrid_food_market_busy': ('Ambiance', 'City'),
    'distant truck starting, departing midway': ('Ambiance', 'City'),
    'Checkout,Voices,Mandarin,Lively': ('Ambiance', 'City'),
    'busy supermarket - cars_trolleys_footsteps_chatter': ('Ambiance', 'City'),
    'train horn and crowd away wide': ('Ambiance', 'City'),
}

RULES = [
    # (top, sub, weight, [keywords])
    # ---- Gunshots ----
    ("Gunshots","Pistol",3,["pistol","handgun","glock","beretta","revolver","9mm","cal pistol","luger","1911"]),
    ("Gunshots","Revolver",3,["revolver","magnum .357","colt python","357"]),
    ("Gunshots","Automatic",3,["automatic","full auto","burst","machine pistol","uzi","smg"]),
    ("Gunshots","Machine_Gun",3,["machine gun","machinegun","ak47","ak 47","ak-47","m249","minigun","gatling","hmg","lmg"]),
    ("Gunshots","Shotgun",3,["shotgun","saiga","pump action","1301","spas"]),
    ("Gunshots","Sniper",3,["sniper","dragunov","svd","barrett","50 cal","bolt action rifle","awp"]),
    ("Gunshots","Rifle",2,["rifle shot","rifle fire","assault rifle","m4","carbine","ar15"]),
    ("Gunshots","SciFi",3,["laser shot","energy weapon","plasma","blaster","ray gun","sci fi weapon","alien weapon","phaser","lightsaber"]),
    ("Gunshots","Misc",1,["gunshot","gun shot","gun fire","gunfire","shot","firing","suppressed","silenced"]),
    # ---- Weapons (other) ----
    ("Weapons","Reloads",3,["reload","magazine","mag in","mag out","magazine in","magazine out","charging handle","bolt action","slide rack","cocking","weapon handling","weapon foley","gun handling","holster"]),
    ("Weapons","Melee",3,["melee","sword","blade","axe","katana","machete","knife","slash","stab","punch","punching","fist","bat hit","club","whip","spear"]),
    ("Weapons","Bows",3,["bow","arrow","crossbow","arrow impact"]),
    ("Weapons","Bullet_Impacts",3,["bullet impact","ricochet","ricochet","whizz","bullet whiz","shell casing","casing drop","bullet drop"]),
    ("Weapons","Handling",2,["weapon","firearm","gun","ammo","artillery","cannon","tank gun","gun lock"]),
    # ---- Explosions ----
    ("Explosions","Fireworks",3,["firework","firecracker","firework","rocket launch"]),
    ("Explosions","Debris",2,["debris","collapse","rubble","demolition"]),
    ("Explosions","Explosions",3,["explosion","explode","bomb","blast","grenade","detonation","dynamite","tnt","mortar","mine explosion"]),
    # ---- Impacts ----
    ("Impacts","Metal",2,["metal","steel","iron","pipe clang","clang","clank","metal hit","metal impact","metal plate"]),
    ("Impacts","Glass",3,["glass","shatter","window break","glass break","glass smash"]),
    ("Impacts","Wood",2,["wood","wooden","timber","wood hit","wood impact"]),
    ("Impacts","Stone",2,["stone","rock","concrete","brick","boulder","rubble"]),
    ("Impacts","Plastic",2,["plastic","pvc","styrofoam"]),
    ("Impacts","Generic",1,["impact","smash","crash","slam","thud","bang","hit","smack","whack"]),
    # ---- Foley ----
    ("Foley","Doors",3,["door","gate","drawer","hatch","latch","door handle","squeaky gate","creaking door"]),
    ("Foley","Cloth",3,["cloth","fabric","zipper","velcro","leather","neoprene","t shirt","t-shirt","clothing"]),
    ("Foley","Paper",3,["paper","cardboard","newspaper","page turn","books"]),
    ("Foley","Tools",3,["tool","drill","saw","screwdriver","hammer","wrench","lawn mower","construction tool"]),
    ("Foley","Kitchen",2,["kitchen","cutlery","dish","cook","fridge","blender","glassware","bottle","water pour","bathroom","toilet","faucet","shower"]),
    ("Foley","Objects",1,["foley","drop","pickup","object","prop","cup","coin","key","bubble","ball","toy"]),
    # ---- Footsteps ----
    ("Footsteps","Grass",3,["footstep grass","grass footstep","grass step"]),
    ("Footsteps","Gravel",3,["gravel","pebbles","rubble footstep"]),
    ("Footsteps","Concrete",3,["concrete step","concrete footstep","pavement","asphalt step"]),
    ("Footsteps","Wood",3,["wood step","wooden floor footstep","floorboard"]),
    ("Footsteps","Metal",3,["metal step","metal footstep","barefoot on metal"]),
    ("Footsteps","Water",3,["water step","puddle step","water footstep"]),
    ("Footsteps","Snow",3,["snow step","snow footstep"]),
    ("Footsteps","Dirt",3,["dirt step","sand step","soil step"]),
    ("Footsteps","Misc",2,["footstep","foot step","step","walking","walk","run","sprint","barefoot","shoe","boot","movement"]),
    # ---- Vehicles ----
    ("Vehicles","Motorcycles",3,["motorcycle","motorbike","suzuki","harley","yamaha","atv","quad bike","intruder"]),
    ("Vehicles","Boats",3,["boat","ship","yacht","marine","ferry","mercury 4-stroke","lake boat","harbor boat"]),
    ("Vehicles","Aircraft",3,["aircraft","helicopter","plane","jet","drone","skydiving","parachute","jetpack"]),
    ("Vehicles","Trains",3,["train","railway","subway","metro","locomotive","tram","train station","train onboard"]),
    ("Vehicles","Tanks",3,["tank","apc","bandkanon","artillery vehicle","armored vehicle"]),
    ("Vehicles","Engines",2,["engine","motor","idle","rev","turbine","exhaust"]),
    ("Vehicles","Cars",2,["car","auto","audi","citroen","renault","mustang","corvette","bmw","ford","fiat","peugeot","porsche","ferrari","mclaren","race car","racing","drift","vehicle","truck","van","cadillac","morris","range rover","wrangler"]),
    # ---- Ambiance ----
    ("Ambiance","Nature",2,["forest","woods","creek","river","jungle","nature ambience","norway","countryside ambience","field recording","birds","mountain"]),
    ("Ambiance","City",2,["city","street","urban","traffic","market","downtown","new york","paris","london","dublin","subway station","sidewalk"]),
    ("Ambiance","Crowd",2,["crowd","walla","party","people","concert","stadium","cafe","restaurant","battle crowd","chant","cheer"]),
    ("Ambiance","Interior",2,["interior","office","indoor","house","home","kitchen","room tone","roomtone","supermarket","shop","mall","airport","station","bathroom","church","gym","swimming pool","building","warehouse interior"]),
    ("Ambiance","Industrial",2,["industrial","factory","warehouse","construction","harbor","steelwork","machine room","power plant"]),
    ("Ambiance","Rural",2,["rural","countryside","suburb","village","farm","desert","canyon"]),
    ("Ambiance","Space",3,["space ambience","spaceship","space station","cosmic","galaxy","nebula"]),
    ("Ambiance","SciFi",2,["sci fi ambience","alien ambience","ship interior","mothership"]),
    ("Ambiance","Horror",2,["haunted","creepy","morgue","sinister ambience","chamber of shadows","dark ambience"]),
    ("Ambiance","Misc",1,["ambience","ambiance","room tone","roomtone","atmos","atmosphere","background","environment","locations","location"]),
    # ---- Nature ----
    ("Nature","Fire",3,["fire","flame","campfire","torch","firecracker fire","burning","candle","fire crackle"]),
    ("Nature","Wind",2,["wind","breeze","gust","wind in trees"]),
    ("Nature","Birds",2,["bird","crow","seagull","owl","red kite","pigeon","duck","chicken"]),
    ("Nature","Animals",2,["animal","dog","cat","cow","pig","sheep","horse","wolf","deer","bat","frog","insect","farm animals"]),
    ("Nature","Water",2,["water","stream","creek","river","ocean","sea","wave","splash","underwater","liquid","waterfall","drip","submerge","shore waves","flowing water","boiling"]),
    ("Nature","Insects",2,["insect","bee","fly","mosquito","cricket","cicada"]),
    # ---- Weather ----
    ("Weather","Rain",3,["rain","rainfall","downpour","drizzle"]),
    ("Weather","Thunder",3,["thunder","lightning"]),
    ("Weather","Storm",3,["storm","hurricane","blizzard"]),
    ("Weather","Snow",3,["snow","blizzard","ice"]),
    ("Weather","Wind",2,["wind","gale","wind ambience"]),
    # ---- Animals ----
    ("Animals","Monsters",3,["monster","creature","zombie","ghost","demon","dragon","wither","ghast","mutant","wendigo","godzilla"]),
    ("Animals","Birds",2,["bird","crow","owl","seagull"]),
    ("Animals","Mammals",2,["dog","cat","cow","pig","sheep","horse","wolf","deer","bat","rabbit","fox","lion","monkey","bear"]),
    ("Animals","Reptiles",2,["snake","lizard","frog","crocodile","frog","dragon"]),
    ("Animals","Insects",2,["insect","bee","fly","spider","bug","cricket"]),
    # ---- Human ----
    ("Human","Voice",3,["voice","dialogue","speech","walla","vocal","vocals","talk","conversation","announcement","pa system","radio host"]),
    ("Human","Efforts",3,["grunt","effort","scream","pain","breath","breathing","exhale","inhale","yawn","sigh","laugh","laughter","cry","baby","cough","sneeze"]),
    ("Human","Body",3,["fart","burp","stomach","snore","heartbeat","chew","eat","kiss","lick","punch body"]),
    ("Human","Crowd",2,["crowd","applause","clap","cheer","boo","chant","children","kids"]),
    # ---- SciFi ----
    ("SciFi","Weapons",3,["laser","plasma","energy weapon","blaster","ray gun","sci fi weapon","alien weapon","phaser","lightsaber","railgun"]),
    ("SciFi","UI",3,["futuristic user interface","sci fi ui","hologram","quantum ui","user interface","kawaii ui","computer interface"]),
    ("SciFi","Robots",3,["robot","android","droid","mech","cyborg","robotic"])  ,
    ("SciFi","Voices",3,["sci fi voice","alien voice","android voice","robot voice","vocoder","mothership","big battle robot","advanced android"]),
    ("SciFi","Alarms",3,["sci fi alarm","alien alarm","spaceship alarm"]),
    ("SciFi","Drones",3,["drone","hover","flying saucer","ufo"]),
    ("SciFi","Textures",2,["alien","sci fi texture","alien texture","space","starship","portal","cyber","futuristic","extra terrestrial"]),
    # ---- UI ----
    ("UI","Computers",3,["computer","keyboard","mouse","typewriter","terminal","modem","hdd","printer","hard drive","floppy","retrofuturistic computer","morse code"]),
    ("UI","Buttons",2,["button","switch","click","toggle","lever","blip","beep","key press"]),
    ("UI","Notifications",3,["notification","alert","message","pop","confirm","error beep","success","ui sound","ui notification"]),
    ("UI","Misc",2,["ui","interface","menu","start select","start_","cursor"]),
    # ---- Machines ----
    ("Machines","Industrial",2,["industrial","factory","conveyor","machinery","turbine","piston","generator","geyser"]),
    ("Machines","Electric",3,["electric","electricity","voltage","spark","zap","hum buzz","high voltage","power line","electromagnetic"]),
    ("Machines","Robot",2,["robot","servo","droid","robotic"]),
    ("Machines","Steampunk",3,["steampunk","gear","clockwork","clock","pneumatic","winding"]),
    ("Machines","Mechanisms",2,["mechanism","gear","lever","valve","ratchet","squeak","creak","cog","pulley","small mechanism","tiny gears","industrial lever","locker"]),
    ("Machines","Doors",2,["door","gate","hatch","drawer","squeaky gate"]),
    ("Machines","Gadgets",2,["gadget","device","flask","hair dryer","machine","appliance","printer press"]),
    # ---- Transitions ----
    ("Transitions","Whooshes",3,["whoosh","swoosh","pass by","flyby","doppler","whoosh by"]),
    ("Transitions","Risers",3,["riser","swell","uplifter","tension","build up","rising"]),
    ("Transitions","Cinematic",2,["transition","cinematic","trailer","braam","cinematic impact","drone swarm"]),
    # ---- Music ----
    ("Music","Loops",3,["bpm","loop","beat","music loop","artlist","suno","backing track","song"]),
    ("Music","Instruments",2,["guitar","drum","synth","bass","piano","orchestra","percussion","violin","strings","horn"]),
    ("Music","Stingers",2,["stinger","music spill","melody","jingle","theme"]),
    ("Music","Misc",1,["music","musik","soundtrack"]),
    # ---- Horror ----
    ("Horror","Gore",3,["gore","blood","flesh","cut flesh","torturing","bone cracking","ripping","tearing","rip","flesh"]),
    ("Horror","Monsters",2,["monster","creature growl","zombie","ghost","demon","wendigo","sinister","mutant"]),
    ("Horror","Scary",2,["horror","scary","haunted","creepy","dark texture","eerie","dread","terror"]),
    ("Horror","Magic",3,["magic","spell","magical","supernatural","wizard","fairy","enchanted","sorcery","rune"]),
    # ---- Alarms ----
    ("Alarms","Alarms",3,["alarm","siren","warning","klaxon","buzzer","emergency","police siren","ambulance"]),
    # ---- extra coverage ----
    ("Animals","Monsters",4,["mob","creeper","enderman","ender dragon","ghast","wither","blaze","slime","magma cube","guardian","silverfish","skeleton","zombie","creature","mutant","wendigo"]),
    ("Animals","Mammals",3,["bat","cow","pig","sheep","wolf","rabbit","fox","villager","iron golem","goat","bear","meow","hiss","bark","moo","oink","neigh","growl","purr"]),
    ("Animals","Birds",3,["chicken","parrot","wings","wing flap"]),
    ("Ambiance","Cave",3,["cave","underground","dungeon","sewer","tunnel"]),
    ("Ambiance","Misc",2,["ambient"]),
    ("Vehicles","Trains",4,["minecart","trolley","cart"]),
    ("Machines","Mechanisms",3,["piston","pump","crank","tile"]),
    ("SciFi","Textures",3,["portal","nexus","glitch","rune"]),
    ("Music","Instruments",3,["note","flute","harp","bell","chime"]),
    ("Foley","Objects",2,["random","toggle","pickup","generic","dig"]),
    ("Impacts","Generic",3,["damage","hurt","death","fall","shatter"]),
    # ---- more coverage ----
    ("UI","Misc",3,["user interaction","interaction","activation","window open","progress","feedback"]),
    ("UI","Buttons",2,["select","click","toggle"]),
    ("SciFi","Textures",2,["parallax","vortex","geodrone","essential scifi","scimisc","ping","burst"]),
    ("Ambiance","City",2,["town","city traffic","traffic","bridge"]),
    ("Ambiance","Interior",2,["bunker","school","classroom","whiteboard"]),
    ("Nature","Water",2,["justwater"]),
    ("Vehicles","Trains",2,["passing trains","rails","tram"]),
    ("Machines","Industrial",2,["wind turbine","turbine","ventilation"]),
    ("Machines","Electric",2,["hum buzz","buzz tone","cadenced buzz","newsreel","hum","buzz","crackle"]),
    ("Vehicles","Aircraft",2,["beechcraft","take off","glider"]),
    ("Gunshots","Revolver",3,["smith","wesson","chiefs special","38 special"]),
    ("Gunshots","Pistol",3,["hammerli","22lr","22 lr"]),
    ("Vehicles","Motorcycles",2,["polaris","sportsman","quad"]),
    ("Foley","Objects",2,["slapstick","comedy","goofy","xylophone","scrape","anime","cartoon","comedic","power up"]),
    ("Human","Body",2,["crepitus","knuckle"]),
    ("Machines","Mechanisms",2,["beam barrier","expgap"]),
    ("Weather","Snow",2,["ski"]),
    ("Foley","Kitchen",3,["bread","pizza","onion","leek","food","celery","buckwheat","pasta","egg","fruit","vegetable","meal"]),
    ("Foley","Paper",3,["cellophane","tape noise"]),
    ("Foley","Cloth",2,["jacket","handbag","towel","velvet","curtain"]),
    ("Foley","Objects",2,["trash","cigarette","chocolate","crush box","abacus","fingernail","nails","sculpture","grit","scatter","spill","marbles"]),
    ("Weapons","Melee",3,["switchblade","unsheath","sheath"]),
    ("Music","Instruments",3,["zither","cello","violin","trumpet","sax","organ"]),
    ("Machines","Electric",2,["tape","radio","scanner"]),
    ("Machines","Industrial",2,["printing press","hydraulics","grinding","whirring"]),
    ("Ambiance","Interior",3,["parking garage","garage","church","cathedral","hall"]),
    ("Nature","Water",2,["beach","seaside","shore"]),
    ("Animals","Mammals",3,["seal","seals"]),
    ("Vehicles","Motorcycles",2,["ducati"]),
    ("Vehicles","Cars",1,["pole position"]),
    ("Transitions","Whooshes",3,["passby"]),
    ("SciFi","Textures",2,["lightbike","light bike"]),
]

def norm(s):
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    s = s.lower()
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", s)
    s = re.sub(r"[_\-/\\|,]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s

PATTERNS = [(top, sub, w, [re.compile(r"\b" + re.escape(k.lower()) + r"s?\b") for k in kws]) for top, sub, w, kws in RULES]

def classify(rel):
    folder = norm(os.path.dirname(rel))
    fname = norm(os.path.splitext(os.path.basename(rel))[0])
    scores = collections.Counter()
    for top, sub, w, pats in PATTERNS:
        s = 0
        for pat in pats:
            if pat.search(folder):
                s += w * 2
            if pat.search(fname):
                s += w
        if s:
            scores[(top, sub)] += s
    if not scores:
        return ("Unsorted", "Misc"), 0
    best = max(scores.items(), key=lambda kv: (kv[1], -len(kv[0][0])))
    return best[0], best[1]

def walk_audio():
    out = []
    for dp, dn, fn in os.walk(ROOT):
        rel_dir = os.path.relpath(dp, ROOT)
        top = rel_dir.split(os.sep)[0]
        if top in SKIP_TOP:
            continue
        for f in fn:
            if f.lower().endswith(AUDIO):
                out.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return out

if __name__ == "__main__":
    files = walk_audio()
    print("audio files:", len(files))
    dist = collections.Counter()
    samples = collections.defaultdict(list)
    for rel in files:
        (t, s), sc = classify(rel)
        dist[(t, s)] += 1
        if len(samples[t]) < 4:
            samples[t].append((rel, s, sc))
    print("--- top-level distribution ---")
    top_c = collections.Counter()
    for (t, s), c in dist.items():
        top_c[t] += c
    for t, c in top_c.most_common():
        print(f"{c:6d}  {t}")
    print("--- sub distribution ---")
    for (t, s), c in sorted(dist.items()):
        print(f"{c:6d}  {t} / {s}")

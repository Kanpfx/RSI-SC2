"""Export compact three-race unit facts from the local SC2 API; no match is played."""
import asyncio
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
HEADER_URL = "https://raw.githubusercontent.com/Blizzard/s2client-api/master/include/sc2api/sc2_typeenums.h"
PROTO_URL = "https://github.com/Blizzard/s2client-proto/blob/master/s2clientprotocol/data.proto"
CACHE = ROOT / "runtime/cache/knowledge_sources"
# Compact selection, not a claim that every returned ID is currently trainable.
SELECTED = set("""
SCV MULE COMMANDCENTER ORBITALCOMMAND PLANETARYFORTRESS SUPPLYDEPOT SUPPLYDEPOTLOWERED
REFINERY BARRACKS FACTORY STARPORT BARRACKSTECHLAB BARRACKSREACTOR FACTORYTECHLAB
FACTORYREACTOR STARPORTTECHLAB STARPORTREACTOR ENGINEERINGBAY ARMORY FUSIONCORE
GHOSTACADEMY BUNKER MISSILETURRET SENSORTOWER MARINE MARAUDER REAPER GHOST HELLION
HELLIONTANK WIDOWMINE WIDOWMINEBURROWED SIEGETANK SIEGETANKSIEGED THOR THORAP
VIKINGFIGHTER VIKINGASSAULT MEDIVAC LIBERATOR LIBERATORAG BANSHEE RAVEN BATTLECRUISER
PROBE NEXUS PYLON ASSIMILATOR GATEWAY WARPGATE CYBERNETICSCORE FORGE PHOTONCANNON
SHIELDBATTERY ROBOTICSFACILITY ROBOTICSBAY STARGATE FLEETBEACON TWILIGHTCOUNCIL
TEMPLARARCHIVE DARKSHRINE ZEALOT STALKER SENTRY ADEPT HIGHTEMPLAR DARKTEMPLAR ARCHON
IMMORTAL COLOSSUS DISRUPTOR OBSERVER OBSERVERSIEGEMODE WARPPRISM WARPPRISMPHASING
PHOENIX VOIDRAY ORACLE TEMPEST CARRIER MOTHERSHIP
DRONE LARVA HATCHERY LAIR HIVE OVERLORD OVERLORDTRANSPORT OVERSEER OVERSEERSIEGEMODE
EXTRACTOR SPAWNINGPOOL ROACHWARREN BANELINGNEST EVOLUTIONCHAMBER HYDRALISKDEN
LURKERDENMP INFESTATIONPIT SPIRE GREATERSPIRE ULTRALISKCAVERN NYDUSNETWORK NYDUSCANAL
SPINECRAWLER SPORECRAWLER CREEPTUMOR QUEEN ZERGLING BANELING ROACH RAVAGER HYDRALISK
LURKERMP LURKERMPBURROWED INFESTOR SWARMHOSTMP LOCUSTMP MUTALISK CORRUPTOR BROODLORD
ULTRALISK VIPER
""".split())


def parse_header(text):
    """Read Blizzard's common 1v1 roster and documented unit/ability associations."""
    units, abilities = {}, {}
    section = None
    for line in text.splitlines():
        if "enum class UNIT_TYPEID {" in line:
            section = "units"
        elif "enum class ABILITY_ID {" in line:
            section = "abilities"
        elif "};" in line:
            section = None
        match = re.match(r"\s*(\w+)\s*=\s*(\d+),\s*(?://\s*(.*))?", line)
        if not match:
            continue
        name, number, comment = match.groups()
        if section == "units" and name.startswith(("TERRAN_", "PROTOSS_", "ZERG_")):
            units[int(number)] = (name.split("_", 1)[1], set((comment or "").split(", ")))
        elif section == "abilities":
            abilities[name] = int(number)
    return units, abilities


def build_units(data, header, client, map_name):
    """Keep raw API values; do not invent missing costs, prerequisites or ability gates."""
    from s2clientprotocol import common_pb2, data_pb2
    roster, enum_abilities = parse_header(header)
    types = {unit.unit_id: unit for unit in data.units}
    def label(unit):
        return roster[unit.unit_id][0] if unit.unit_id in roster else unit.name.upper()
    valid = {key: value for key, value in types.items() if label(value) in SELECTED
             and value.available and value.race in (1, 2, 3)}
    records = {}
    def supplied(proto, key):
        return getattr(proto, key) if proto.HasField(key) else None
    for identity, unit in sorted(valid.items()):
        name = label(unit)
        producers = [roster[key][0] for key in valid if key in roster and unit.ability_id and any(
            enum_abilities.get(ability) == unit.ability_id for ability in roster[key][1])]
        def named(key):
            return roster[key][0] if key in roster else types[key].name if key in types else f"unit_id:{key}"
        row = {"id": identity, "race": common_pb2.Race.Name(unit.race),
            "api_cost": [supplied(unit, "mineral_cost"), supplied(unit, "vespene_cost")],
            "supply": supplied(unit, "food_required"),
            "build_time_api": supplied(unit, "build_time"), "creation_ability": supplied(unit, "ability_id"),
            "producer": sorted(producers) or None,
            "requires_api": [named(unit.tech_requirement)] if unit.tech_requirement else None,
            "require_attached": unit.require_attached if unit.HasField("require_attached") else None,
            "attributes": [data_pb2.Attribute.Name(value) for value in unit.attributes],
            "speed_api": supplied(unit, "movement_speed"), "sight": supplied(unit, "sight_range"),
            "armor": supplied(unit, "armor")}
        if unit.food_provided:
            row["supply_provided"] = unit.food_provided
        if unit.tech_alias:
            row["tech_alias"] = [named(key) for key in unit.tech_alias]
        if unit.unit_alias:
            row["unit_alias"] = named(unit.unit_alias)
        if unit.weapons:
            row["weapons"] = [{"target": data_pb2.Weapon.TargetType.Name(weapon.type),
                "damage": weapon.damage, "hits": weapon.attacks, "range": weapon.range,
                "period_api": supplied(weapon, "speed"),
                **({"bonus": {data_pb2.Attribute.Name(b.attribute): b.bonus for b in weapon.damage_bonus}}
                   if weapon.damage_bonus else {})} for weapon in unit.weapons]
        records[name] = row
        row.setdefault("weapons", None)
    return {"_meta": {"source": PROTO_URL, "client": client, "map": map_name,
        "roster_source": HEADER_URL, "roster_sha256": hashlib.sha256(header.encode()).hexdigest(),
        "scope": "Selected three-race units/buildings and essential forms; client available does not prove current trainability.",
        "fields": {"api_cost": "Raw UnitTypeData [minerals,gas], NOT per-order cost; morph and batch costs differ.",
            "producer": "Official header association for exact creation ability; null = unavailable; alternatives may be incomplete.",
            "requires_api": "Partial tech_requirement only; null = not supplied, NOT no requirements; require_attached applies only to this field.",
            "api_units": "build_time_api/speed_api/period_api retain game-data units; no implicit ladder-time conversion."},
        "limits": "No HP/shield/detection/cooldown/energy costs in UnitTypeData; production rules incomplete. weapons=null is unknown (may use turrets/abilities), NOT unarmed."},
        "units": records}


def write_units(payload, raw):
    payload["_meta"]["response_sha256"] = hashlib.sha256(raw).hexdigest()
    target = ROOT / "knowledge/sc2/units.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ['{', '  "_meta": ' + json.dumps(payload["_meta"], ensure_ascii=False) + ',', '  "units": {']
    rows = list(payload["units"].items())
    lines += ['    ' + json.dumps(name) + ': ' + json.dumps(row, ensure_ascii=False, separators=(",", ":")) +
              ("," if index < len(rows)-1 else "") for index, (name, row) in enumerate(rows)]
    target.write_text("\n".join(lines + ['  }', '}']) + "\n", encoding="utf-8")
    print(json.dumps({"file": str(target), "count": len(rows),
        "races": dict(Counter(row["race"] for _, row in rows)), "client": payload["_meta"]["client"]}, indent=2))


async def export(config, header):
    os.environ["SC2PATH"] = config["sc2_path"]
    from sc2 import maps
    from sc2.bot_ai import BotAI
    from sc2.client import Client
    from sc2.data import Difficulty, Race
    from sc2.player import Bot, Computer
    from sc2.sc2process import SC2Process
    from s2clientprotocol import sc2api_pb2

    async with SC2Process() as controller:
        await controller.create_game(maps.get(config["check_map"]),
            [Bot(Race.Terran, BotAI()), Computer(Race.Zerg, Difficulty.VeryEasy)], realtime=False, random_seed=1)
        client = Client(controller._ws)
        try:
            await client.join_game(race=Race.Terran)
            ping = (await client.ping()).ping
            if ping.base_build != config["sc2_build"]:
                raise ValueError(f"Unexpected SC2 build: {ping.base_build}")
            response = await client._execute(data=sc2api_pb2.RequestData(unit_type_id=True, ability_id=True, upgrade_id=True))
            version = {name: getattr(ping, name) for name in ("game_version", "base_build", "data_build", "data_version")}
            payload = build_units(response.data, header, version, config["check_map"])
            CACHE.mkdir(parents=True, exist_ok=True)
            raw = response.data.SerializeToString(deterministic=True)
            (CACHE / "response_data.bin").write_bytes(raw)
            (CACHE / "client.json").write_text(json.dumps({"client": version, "map": config["check_map"],
                "response_sha256": hashlib.sha256(raw).hexdigest()}), encoding="utf-8")
            write_units(payload, raw)
        finally:
            await client.leave()


def main():
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-cache", action="store_true", help="Rebuild from the last captured response; do not launch SC2")
    args = parser.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    header_path = CACHE / "sc2_typeenums.h"
    if not header_path.exists():
        if args.from_cache:
            raise FileNotFoundError("Cached official header is missing; run a fresh export")
        with urlopen(HEADER_URL, timeout=30) as response:
            header_path.write_bytes(response.read())
    config = json.loads((ROOT / "environment.json").read_text(encoding="utf-8"))
    if args.from_cache:
        from s2clientprotocol import sc2api_pb2
        info = json.loads((CACHE / "client.json").read_text(encoding="utf-8"))
        raw = (CACHE / "response_data.bin").read_bytes()
        if hashlib.sha256(raw).hexdigest() != info["response_sha256"]:
            raise ValueError("Cached client metadata does not match response")
        write_units(build_units(sc2api_pb2.ResponseData.FromString(raw),
            header_path.read_text(encoding="utf-8"), info["client"], info["map"]), raw)
        return
    asyncio.run(asyncio.wait_for(export(config, header_path.read_text(encoding="utf-8")), timeout=60))


if __name__ == "__main__":
    main()

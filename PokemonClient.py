import asyncio
import json
import time
import os
import bsdiff4
import subprocess
import zipfile
import hashlib
from asyncio import StreamReader, StreamWriter
from typing import List


import Utils
from Utils import async_start
from CommonClient import CommonContext, server_loop, gui_enabled, ClientCommandProcessor, logger, \
    get_base_parser

from worlds.pokemon_rb.locations import location_data

location_map = {"Rod": {}, "EventFlag": {}, "Missable": {}, "Hidden": {}, "list": {}}
location_bytes_bits = {}
for location in location_data:
    if location.ram_address is not None:
        if type(location.ram_address) == list:
            location_map[type(location.ram_address).__name__][(location.ram_address[0].flag, location.ram_address[1].flag)] = location.address
            location_bytes_bits[location.address] = [{'byte': location.ram_address[0].byte, 'bit': location.ram_address[0].bit},
                                                     {'byte': location.ram_address[1].byte, 'bit': location.ram_address[1].bit}]
        else:
            location_map[type(location.ram_address).__name__][location.ram_address.flag] = location.address
            location_bytes_bits[location.address] = {'byte': location.ram_address.byte, 'bit': location.ram_address.bit}

SYSTEM_MESSAGE_ID = 0

CONNECTION_TIMING_OUT_STATUS = "Connection timing out. Please restart your emulator, then restart pkmn_rb.lua"
CONNECTION_REFUSED_STATUS = "Connection Refused. Please start your emulator and make sure pkmn_rb.lua is running"
CONNECTION_RESET_STATUS = "Connection was reset. Please restart your emulator, then restart pkmn_rb.lua"
CONNECTION_TENTATIVE_STATUS = "Initial Connection Made"
CONNECTION_CONNECTED_STATUS = "Connected"
CONNECTION_INITIAL_STATUS = "Connection has not been initiated"

DISPLAY_MSGS = True


class GBCommandProcessor(ClientCommandProcessor):
    def __init__(self, ctx: CommonContext):
        super().__init__(ctx)

    def _cmd_gb(self):
        """Check Gameboy Connection State"""
        if isinstance(self.ctx, GBContext):
            logger.info(f"Gameboy Status: {self.ctx.gb_status}")


class GBContext(CommonContext):
    command_processor = GBCommandProcessor
    game = 'Pokemon Red and Blue'
    items_handling = 0b101

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        self.gb_streams: (StreamReader, StreamWriter) = None
        self.gb_sync_task = None
        self.messages = {}
        self.locations_array = None
        self.gb_status = CONNECTION_INITIAL_STATUS
        self.awaiting_rom = False
        self.display_msgs = True

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super(GBContext, self).server_auth(password_requested)
        if not self.auth:
            self.awaiting_rom = True
            logger.info('Awaiting connection to Bizhawk to get Player information')
            return

        await self.send_connect()

    def _set_message(self, msg: str, msg_id: int):
        if DISPLAY_MSGS:
            self.messages[(time.time(), msg_id)] = msg

    def on_package(self, cmd: str, args: dict):
        if cmd == 'Connected':
            self.locations_array = None
        elif cmd == "RoomInfo":
            self.seed_name = args['seed_name']
        elif cmd == 'Print':
            msg = args['text']
            if ': !' not in msg:
                self._set_message(msg, SYSTEM_MESSAGE_ID)
        elif cmd == "ReceivedItems":
            msg = f"Received {', '.join([self.item_names[item.item] for item in args['items']])}"
            self._set_message(msg, SYSTEM_MESSAGE_ID)

    def run_gui(self):
        from kvui import GameManager

        class GBManager(GameManager):
            logging_pairs = [
                ("Client", "Archipelago")
            ]
            base_title = "Archipelago Pokémon Client"

        self.ui = GBManager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="UI")


def get_payload(ctx: GBContext):
    current_time = time.time()
    return json.dumps(
        {
            "items": [item.item for item in ctx.items_received],
            "messages": {f'{key[0]}:{key[1]}': value for key, value in ctx.messages.items()
                         if key[0] > current_time - 10}
        }
    )


async def parse_locations(data: List, ctx: GBContext):
    locations = []
    flags = {"EventFlag": data[:0x140], "Missable": data[0x140:0x140 + 0x20],
             "Hidden": data[0x140 + 0x20: 0x140 + 0x20 + 0x0E], "Rod": data[0x140 + 0x20 + 0x0E:]}

    # Check for clear problems
    if len(flags['Rod']) > 1:
        return
    if flags["EventFlag"][1] + flags["EventFlag"][8] + flags["EventFlag"][9] + flags["EventFlag"][12] \
            + flags["EventFlag"][61] + flags["EventFlag"][62] + flags["EventFlag"][63] + flags["EventFlag"][64] \
            + flags["EventFlag"][65] + flags["EventFlag"][66] + flags["EventFlag"][67] + flags["EventFlag"][68] \
            + flags["EventFlag"][69] + flags["EventFlag"][70] != 0:
        return

    for flag_type, loc_map in location_map.items():
        for flag, loc_id in loc_map.items():
            if flag_type == "list":
                if (flags["EventFlag"][location_bytes_bits[loc_id][0]['byte']] & 1 << location_bytes_bits[loc_id][0]['bit']
                        and flags["Missable"][location_bytes_bits[loc_id][1]['byte']] & 1 << location_bytes_bits[loc_id][1]['bit']):
                    locations.append(loc_id)
            elif flags[flag_type][location_bytes_bits[loc_id]['byte']] & 1 << location_bytes_bits[loc_id]['bit']:
                locations.append(loc_id)
    if flags["EventFlag"][280] & 1 and not ctx.finished_game:
        await ctx.send_msgs([
                    {"cmd": "StatusUpdate",
                     "status": 30}
                ])
        ctx.finished_game = True
    if locations == ctx.locations_array:
        return
    ctx.locations_array = locations
    if locations is not None:
        await ctx.send_msgs([{"cmd": "LocationChecks", "locations": locations}])


async def gb_sync_task(ctx: GBContext):
    logger.info("Starting GB connector. Use /gb for status information")
    while not ctx.exit_event.is_set():
        error_status = None
        if ctx.gb_streams:
            (reader, writer) = ctx.gb_streams
            msg = get_payload(ctx).encode()
            writer.write(msg)
            writer.write(b'\n')
            try:
                await asyncio.wait_for(writer.drain(), timeout=1.5)
                try:
                    # Data will return a dict with up to two fields:
                    # 1. A keepalive response of the Players Name (always)
                    # 2. An array representing the memory values of the locations area (if in game)
                    data = await asyncio.wait_for(reader.readline(), timeout=5)
                    data_decoded = json.loads(data.decode())
                    #print(data_decoded)

                    if ctx.seed_name and ctx.seed_name != ''.join([chr(i) for i in data_decoded['seedName'] if i != 0]):
                        msg = "The server is running a different multiworld than your client is. (invalid seed_name)"
                        logger.info(msg, extra={'compact_gui': True})
                        ctx.gui_error('Error', msg)
                        error_status = CONNECTION_RESET_STATUS
                    ctx.seed_name = ''.join([chr(i) for i in data_decoded['seedName'] if i != 0])
                    if not ctx.auth:
                        ctx.auth = ''.join([chr(i) for i in data_decoded['playerName'] if i != 0])
                        if ctx.auth == '':
                            logger.info("Invalid ROM detected. No player name built into the ROM.")
                        if ctx.awaiting_rom:
                            await ctx.server_auth(False)
                    if 'locations' in data_decoded and ctx.game and ctx.gb_status == CONNECTION_CONNECTED_STATUS \
                            and not error_status and ctx.auth:
                        # Not just a keep alive ping, parse
                        async_start(parse_locations(data_decoded['locations'], ctx))
                except asyncio.TimeoutError:
                    logger.debug("Read Timed Out, Reconnecting")
                    error_status = CONNECTION_TIMING_OUT_STATUS
                    writer.close()
                    ctx.gb_streams = None
                except ConnectionResetError as e:
                    logger.debug("Read failed due to Connection Lost, Reconnecting")
                    error_status = CONNECTION_RESET_STATUS
                    writer.close()
                    ctx.gb_streams = None
            except TimeoutError:
                logger.debug("Connection Timed Out, Reconnecting")
                error_status = CONNECTION_TIMING_OUT_STATUS
                writer.close()
                ctx.gb_streams = None
            except ConnectionResetError:
                logger.debug("Connection Lost, Reconnecting")
                error_status = CONNECTION_RESET_STATUS
                writer.close()
                ctx.gb_streams = None
            if ctx.gb_status == CONNECTION_TENTATIVE_STATUS:
                if not error_status:
                    logger.info("Successfully Connected to Gameboy")
                    ctx.gb_status = CONNECTION_CONNECTED_STATUS
                else:
                    ctx.gb_status = f"Was tentatively connected but error occured: {error_status}"
            elif error_status:
                ctx.gb_status = error_status
                logger.info("Lost connection to Gameboy and attempting to reconnect. Use /gb for status updates")
        else:
            try:
                logger.debug("Attempting to connect to Gameboy")
                ctx.gb_streams = await asyncio.wait_for(asyncio.open_connection("localhost", 17242), timeout=10)
                ctx.gb_status = CONNECTION_TENTATIVE_STATUS
            except TimeoutError:
                logger.debug("Connection Timed Out, Trying Again")
                ctx.gb_status = CONNECTION_TIMING_OUT_STATUS
                continue
            except ConnectionRefusedError:
                logger.debug("Connection Refused, Trying Again")
                ctx.gb_status = CONNECTION_REFUSED_STATUS
                continue


async def run_game(romfile):
    auto_start = Utils.get_options()["pokemon_rb_options"].get("rom_start", True)
    if auto_start is True:
        import webbrowser
        webbrowser.open(romfile)
    elif os.path.isfile(auto_start):
        subprocess.Popen([auto_start, romfile],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def patch_and_run_game(game_version, patch_file, ctx):
    base_name = os.path.splitext(patch_file)[0]
    comp_path = base_name + '.gb'
    with open(Utils.local_path(Utils.get_options()["pokemon_rb_options"][f"{game_version}_rom_file"]), "rb") as stream:
        base_rom = bytes(stream.read())
    try:
        with open(Utils.local_path('lib', 'worlds', 'pokemon_rb', f'basepatch_{game_version}.bsdiff4'), 'rb') as stream:
            base_patch = bytes(stream.read())
    except FileNotFoundError:
        with open(Utils.local_path('worlds', 'pokemon_rb', f'basepatch_{game_version}.bsdiff4'), 'rb') as stream:
            base_patch = bytes(stream.read())
    base_patched_rom_data = bsdiff4.patch(base_rom, base_patch)
    basemd5 = hashlib.md5()
    basemd5.update(base_patched_rom_data)

    with zipfile.ZipFile(patch_file, 'r') as patch_archive:
        with patch_archive.open('delta.bsdiff4', 'r') as stream:
            patch = stream.read()
    patched_rom_data = bsdiff4.patch(base_patched_rom_data, patch)

    written_hash = patched_rom_data[0xFFCB:0xFFDB]
    if written_hash == basemd5.digest():
        with open(comp_path, "wb") as patched_rom_file:
            patched_rom_file.write(patched_rom_data)

        async_start(run_game(comp_path))
    else:
        msg = "Patch supplied was not generated with the same base patch version as this client. Patching failed."
        logger.warning(msg)
        ctx.gui_error('Error', msg)


if __name__ == '__main__':

    Utils.init_logging("PokemonClient")

    options = Utils.get_options()

    async def main():
        parser = get_base_parser()
        parser.add_argument('patch_file', default="", type=str, nargs="?",
                            help='Path to an APRED or APBLUE patch file')
        args = parser.parse_args()

        ctx = GBContext(args.connect, args.password)
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="ServerLoop")
        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()
        ctx.gb_sync_task = asyncio.create_task(gb_sync_task(ctx), name="GB Sync")

        if args.patch_file:
            ext = args.patch_file.split(".")[len(args.patch_file.split(".")) - 1].lower()
            if ext == "apred":
                logger.info("APRED file supplied, beginning patching process...")
                async_start(patch_and_run_game("red", args.patch_file, ctx))
            elif ext == "apblue":
                logger.info("APBLUE file supplied, beginning patching process...")
                async_start(patch_and_run_game("blue", args.patch_file, ctx))
            else:
                logger.warning(f"Unknown patch file extension {ext}")

        await ctx.exit_event.wait()
        ctx.server_address = None

        await ctx.shutdown()

        if ctx.gb_sync_task:
            await ctx.gb_sync_task


    import colorama

    colorama.init()

    asyncio.run(main())
    colorama.deinit()

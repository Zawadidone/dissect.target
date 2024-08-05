import io
import logging
from struct import error as StructError
from typing import Any, BinaryIO, Iterator

from dissect.cstruct import cstruct
from dissect.ole import OLE
from dissect.ole.exceptions import Error as OleError
from dissect.shellitem.lnk import Lnk

from dissect.target import Target
from dissect.target.exceptions import UnsupportedPluginError
from dissect.target.helpers.descriptor_extensions import UserRecordDescriptorExtension
from dissect.target.helpers.record import create_extended_descriptor
from dissect.target.helpers.shell_application_ids import APPLICATION_IDENTIFIERS
from dissect.target.helpers.utils import findall
from dissect.target.plugin import Plugin, export
from dissect.target.plugins.os.windows.lnk import LnkRecord, parse_lnk_file

log = logging.getLogger(__name__)

LNK_GUID = b"\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46"

JumpListRecord = create_extended_descriptor([UserRecordDescriptorExtension])(
    "windows/jumplist",
    [
        ("string", "type"),
        ("string", "application_id"),
        ("string", "application_name"),
        *LnkRecord.target_fields,
    ],
)


custom_destination_def = """
struct header {
    int version;
    int unknown1;
    int unknown2;
    int value_type;
}

struct header_end {
    int number_of_entries;
}

struct header_end_0 {
    uint16  name_length;
    wchar   name[name_length];
    int     number_of_entries;
}

struct entry_header {
    char guid[16];
}

struct footer {
    char magic[4];
}
"""

c_custom_destination = cstruct()
c_custom_destination.load(custom_destination_def)


class AutomaticDestinationFile:
    """Parse Jump List AutomaticDestination file."""

    def __init__(self, fh: BinaryIO):
        self.fh = fh
        self.ole = OLE(self.fh)

    def __iter__(self) -> Iterator[Lnk]:
        for dir_name in self.ole.listdir():
            if dir_name == "DestList":
                continue

            dir = self.ole.get(dir_name)

            for item in dir.open():
                try:
                    yield Lnk(io.BytesIO(item))
                except StructError:
                    continue
                except Exception as e:
                    log.warning("Failed to parse LNK file from directory %s", dir_name)
                    log.debug("", exc_info=e)
                    continue


class CustomerDestinationFile:
    """Parse Jump List CustomDestination file."""

    MAGIC_FOOTER = 0xBABFFBAB
    VERSIONS = [2]

    def __init__(self, fh: BinaryIO):
        self.fh = fh

        self.fh.seek(-4, io.SEEK_END)
        self.footer = c_custom_destination.footer(self.fh.read(4))
        self.magic = int.from_bytes(self.footer.magic, "little")

        self.fh.seek(0, io.SEEK_SET)
        self.header = c_custom_destination.header(self.fh)
        self.version = self.header.version

        if self.header.value_type == 0:
            self.header_end = c_custom_destination.header_end_0(self.fh)
        elif self.header.value_type in [1, 2]:
            self.header_end = c_custom_destination.header_end(self.fh)
        else:
            raise NotImplementedError()

    def __iter__(self) -> Iterator[Lnk]:
        # Searches for all LNK GUID's because the number of entries in the header is not always correct.
        buf = self.fh.read()

        for offset in findall(buf, LNK_GUID):
            try:
                lnk = Lnk(io.BytesIO(buf[offset + len(LNK_GUID) :]))
                yield lnk
            except Exception as e:
                log.warning("Failed to parse LNK file from a CustomDestination file")
                log.debug("", exc_info=e)
                continue


class JumpListPlugin(Plugin):
    def __init__(self, target: Target):
        super().__init__(target)
        self.automatic_destinations = []
        self.custom_destinations = []

        for user_details in self.target.user_details.all_with_home():
            for destination in user_details.home_path.glob(
                "AppData/Roaming/Microsoft/Windows/Recent/CustomDestinations/*.customDestinations-ms"
            ):
                self.custom_destinations.append([destination, user_details.user])

        for user_details in self.target.user_details.all_with_home():
            for destination in user_details.home_path.glob(
                "AppData/Roaming/Microsoft/Windows/Recent/AutomaticDestinations/*.automaticDestinations-ms"
            ):
                self.automatic_destinations.append([destination, user_details.user])

    def check_compatible(self) -> None:
        if not any([self.automatic_destinations, self.custom_destinations]):
            raise UnsupportedPluginError("No Jump List files found")

    def name(self, name: str) -> tuple[Any, str, str]:
        application_id, application_type = name.split(".")
        application_type = application_type.split("-")[0]
        application_name = APPLICATION_IDENTIFIERS.get(application_id)

        return application_name, application_id, application_type

    def custom_destination(self) -> Iterator[JumpListRecord]:
        for destination, user in self.custom_destinations:
            fh = destination.open("rb")

            try:
                custom_destination = CustomerDestinationFile(fh)
            except EOFError:
                continue
            except NotImplementedError:
                self.target.log.warning(
                    "The value_type (%i) of the CustomDestination file is not implemented: %s",
                    custom_destination.header.value_type,
                    destination,
                )
                continue
            except Exception as e:
                self.target.log.warning("Failed to parse CustomDestination header: %s", destination)
                self.target.log.debug("", exc_info=e)
                continue

            if not custom_destination.MAGIC_FOOTER == custom_destination.magic:
                self.target.log.warning("The CustomDestination file has an invalid magic footer: %s", destination)
                continue

            if custom_destination.version not in custom_destination.VERSIONS:
                self.target.log.warning(
                    "The CustomDestination file has an unsupported version %i: %s",
                    destination,
                    custom_destination.version,
                )
                continue

            application_name, application_id, application_type = self.name(destination.name)

            for lnk in custom_destination:
                lnk = parse_lnk_file(self.target, lnk, destination)

                if lnk is None:
                    continue

                yield JumpListRecord(
                    type=application_type,
                    application_name=application_name,
                    application_id=application_id,
                    **lnk._asdict(),
                    _user=user,
                    _target=self.target,
                )

    def automatic_destination(self):
        for destination, user in self.automatic_destinations:
            fh = destination.open("rb")

            application_name, application_id, type = self.name(destination.name)

            try:
                automatic_destination = AutomaticDestinationFile(fh)
            except OleError:
                continue
            except Exception as e:
                self.target.log.warning("Failed to parse AutomaticDestination file: %s", destination)
                self.target.log.debug("", exc_info=e)
                continue

            for lnk in automatic_destination:
                lnk = parse_lnk_file(self.target, lnk, destination)

                if lnk is None:
                    continue

                yield JumpListRecord(
                    type=type,
                    application_name=application_name,
                    application_id=application_id,
                    **lnk._asdict(),
                    _user=user,
                    _target=self.target,
                )

    @export(record=JumpListRecord)
    def jumplist(self) -> Iterator[JumpListRecord]:
        """Return the content of Windows Jump List files.

        Jump List is a Windows feature introduced in Windows 7. It contains information about recently accessed
        applications and files. There are two kind of Jump Lists. The AutomaticDestinations are created automatically
        when a user opens an application or file. The CustomDestination is created when a user pins an application or
         a file in a Jump List.

        References:
            - https://forensics.wiki/jump_lists
            - https://github.com/libyal/dtformats/blob/main/documentation/Jump%20lists%20format.asciidoc
        """

        for entry in self.custom_destination():
            yield entry

        for entry in self.automatic_destination():
            yield entry
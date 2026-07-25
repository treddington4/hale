"""Minimal FIT binary record-boundary walker (Phase 23).

garmin_enrich.py needs to splice new records (Hevy's exercise_title/set messages)
into a real Garmin watch FIT file while leaving every other byte untouched --
critically including the Session message's proprietary/undocumented fields
(Training Effect, Respiration Rate, Recovery HR, Est. Sweat Loss, Body Battery)
that neither fitparse nor fit_tool have named support for, and which fit_tool's
own FitFileBuilder round-trip corrupts when fed a real, complex watch recording
(confirmed: a real file with thousands of vendor-specific GenericMessage/
TimestampCorrelationMessage entries came out of FitFileBuilder.build().to_file()
unparseable -- "invalid local message type" -- even though the same builder
handles hevy2garmin's own simple from-scratch files fine).

The fix: don't round-trip the whole file through any library's object model at
all. Only understand the FIT spec's mechanical record-header + field-size-table
structure well enough to compute *byte lengths* of each record -- this doesn't
require knowing what a message type *means*, just how many bytes it occupies,
which is fully determined by whichever definition record was last seen for its
local message type. Verified against a real 118KB/10,505-record file: this
walker's per-message-type record counts matched fitparse's independent parse
exactly, and the byte-accounting assertion (`pos == end`) held throughout.

Building the *new* content (the exercise_title/set messages themselves) still
goes through hevy2garmin's `fit.generate_fit()` + fit_tool's builder as normal --
that path is proven reliable for simple, purpose-built files. This module only
locates the byte range of interest within the ALREADY-BUILT-AND-VALID output of
that call, and locates/patches one single field (FileId.manufacturer) in the
original -- both narrow, mechanical operations, never a full re-encode.
"""
import struct

FIT_DEFINITION_MASK = 0x40
FIT_DEV_DATA_MASK = 0x20
FIT_COMPRESSED_HEADER_MASK = 0x80
FIT_LOCAL_MESG_TYPE_MASK = 0x0F
FIT_COMPRESSED_LOCAL_MESG_TYPE_MASK = 0x60

# FIT global message numbers (stable across the FIT spec, not affected by any
# particular library's bundled field-name profile).
FILE_ID_MESG_NUM = 0
EXERCISE_TITLE_MESG_NUM = 264
SET_MESG_NUM = 225
FILE_ID_SERIAL_NUMBER_FIELD_NUM = 3
# Matches hevy2garmin's own generate_fit() convention (a fixed placeholder,
# never a real device's serial) -- patched in the spliced file since Garmin's
# upload endpoint rejects it as a "Duplicate Activity" (409) when it still
# carries the original watch's real serial_number alongside the original's
# real start_time/session content. Manufacturer is deliberately left as the
# original watch's real value -- confirmed live that exercise names still
# render correctly without spoofing it (see garmin_enrich.py's own docstring).
PLACEHOLDER_SERIAL_NUMBER = 12345


def parse_header(data: bytes) -> dict:
    header_size = data[0]
    data_size = struct.unpack_from("<I", data, 4)[0]
    if data[8:12] != b".FIT":
        raise ValueError(f"not a FIT file (bad signature: {data[8:12]!r})")
    return {
        "header_size": header_size,
        "data_size": data_size,
        "data_start": header_size,
        "data_end": header_size + data_size,
    }


def walk_records(data: bytes, start: int, end: int) -> list:
    """Returns records in file order: {offset, length, is_definition, local_type,
    global_mesg_num}. Maintains a local-message-type -> field-size-list table so
    each data record's length can be computed from whichever definition was most
    recently seen for that local type (a local type can be redefined mid-file)."""
    local_defs = {}
    records = []
    pos = start
    while pos < end:
        header = data[pos]
        rec_start = pos
        if header & FIT_COMPRESSED_HEADER_MASK:
            local_type = (header & FIT_COMPRESSED_LOCAL_MESG_TYPE_MASK) >> 5
            defn = local_defs[local_type]
            length = 1 + sum(defn["field_sizes"]) + sum(defn["dev_field_sizes"])
            records.append({"offset": rec_start, "length": length, "is_definition": False,
                             "local_type": local_type, "global_mesg_num": defn["global_mesg_num"]})
            pos += length
        elif header & FIT_DEFINITION_MASK:
            local_type = header & FIT_LOCAL_MESG_TYPE_MASK
            has_dev_fields = bool(header & FIT_DEV_DATA_MASK)
            architecture = data[pos + 2]
            endian = ">" if architecture == 1 else "<"
            global_mesg_num = struct.unpack_from(endian + "H", data, pos + 3)[0]
            num_fields = data[pos + 5]
            field_sizes = [data[pos + 6 + i * 3 + 1] for i in range(num_fields)]
            fpos = pos + 6 + num_fields * 3
            dev_field_sizes = []
            if has_dev_fields:
                num_dev_fields = data[fpos]
                fpos += 1
                dev_field_sizes = [data[fpos + i * 3 + 1] for i in range(num_dev_fields)]
                fpos += num_dev_fields * 3
            length = fpos - pos
            local_defs[local_type] = {
                "field_sizes": field_sizes, "dev_field_sizes": dev_field_sizes,
                "global_mesg_num": global_mesg_num, "architecture": architecture,
            }
            records.append({"offset": rec_start, "length": length, "is_definition": True,
                             "local_type": local_type, "global_mesg_num": global_mesg_num})
            pos += length
        else:
            local_type = header & FIT_LOCAL_MESG_TYPE_MASK
            defn = local_defs[local_type]
            length = 1 + sum(defn["field_sizes"]) + sum(defn["dev_field_sizes"])
            records.append({"offset": rec_start, "length": length, "is_definition": False,
                             "local_type": local_type, "global_mesg_num": defn["global_mesg_num"]})
            pos += length
    if pos != end:
        raise ValueError(f"record walk misaligned: ended at {pos}, expected {end}")
    return records


def find_field_offset(data: bytes, def_record: dict, data_record: dict, field_num: int):
    """Given a definition record and a matching data record, returns
    (byte_offset, size, architecture) of one field within that data record's
    bytes, or None if the definition doesn't include it."""
    pos = def_record["offset"]
    num_fields = data[pos + 5]
    fpos = pos + 6
    field_defs = []
    for _ in range(num_fields):
        field_defs.append((data[fpos], data[fpos + 1]))  # (field_num, size)
        fpos += 3
    architecture = data[pos + 2]
    data_pos = data_record["offset"] + 1  # skip the 1-byte record header
    for def_num, size in field_defs:
        if def_num == field_num:
            return (data_pos, size, architecture)
        data_pos += size
    return None


def group_byte_span(records: list, global_mesg_num: int):
    """Byte range [start, end) covering every record (definition + data) with
    the given global_mesg_num. Assumes that group is contiguous in the file,
    which holds for hevy2garmin's own from-scratch output (definition
    immediately followed by all its data instances, never interleaved with an
    unrelated message type in between)."""
    matches = [r for r in records if r["global_mesg_num"] == global_mesg_num]
    if not matches:
        return None
    start = min(r["offset"] for r in matches)
    end = max(r["offset"] + r["length"] for r in matches)
    return start, end

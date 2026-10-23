"""lite vendor: 仅导出轻档平台需要的工具，避免拖入 pywidevine 等重依赖。"""
from .data import SongInfo
from .hls import HLSDownloader
from .ip import RandomIPGenerator
from .songinfoutils import SongInfoUtils
from .modulebuilder import BaseModuleBuilder
from .hosts import obtainhostname, hostmatchessuffix
from .importutils import optionalimport, optionalimportfrom
from .cookies import cookies2dict, cookies2string, cachecookies
from .lyric import WhisperLRC, LyricSearchClient, extractdurationsecondsfromlrc, cleanlrc
from .logger import LoggerHandle, colorize, printtable, printfullline, smarttrunctable, cursorpickintable
from .misc import (
    AudioLinkTester, IOUtils, legalizestring, resp2json, isvalidresp,
    safeextractfromdict, usedownloadheaderscookies, useparseheaderscookies,
    usesearchheaderscookies, searchdictbykey, dedupkeeporder, hashablesth, safefunctioncall,
)
from .cmd import (
    CmdArg, CmdOp, CommandBuilder, CommandModsApplier, FFmpegCommandFactory,
    FFprobeCommandFactory, MetaflacCommandFactory, NM3U8DLRECommandFactory,
    MP4BoxCommandFactory, Mp4DecryptCommandFactory, AmdecryptCommandFactory,
    FFprobeAudioCodecCommand, ExtractAudioFromVideoFFmpegCommand,
    ConvertImageToJpegFFmpegCommand, FFmpegDecryptRemuxCommand,
    MetaflacBlockCommand, MetaflacListPictureCommand, MetaflacRemovePictureCommand,
    MetaflacExportPictureCommand, MetaflacImportPictureCommand,
    NM3U8DLREDownloadCommand, MP4BoxAddCommand, Mp4DecryptCommand, AmdecryptCommand,
)

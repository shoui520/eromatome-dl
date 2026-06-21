from __future__ import annotations

from eromatome_dl.sites.base import SiteAdapter
from eromatome_dl.sites.matome import (
    AdamanEroAdapter,
    BakufuAdapter,
    BeppinGirlAdapter,
    DebusenAdapter,
    EchiechiGazouAdapter,
    EroconAdapter,
    EromanidcAdapter,
    ErogazouGalleryAdapter,
    ErokanAdapter,
    EroGazouAdapter,
    EropuruAdapter,
    FemmedollAdapter,
    GenericMatomeAdapter,
    HentaiWitchAdapter,
    HnaladyAdapter,
    IchinukeAdapter,
    ItaDoAdapter,
    KimootokoAdapter,
    KyarabetsuNijieroAdapter,
    LesKokoAdapter,
    LoveliveforeverAdapter,
    MegamichAdapter,
    NijiPinkAdapter,
    NijimoeEroGazouAdapter,
    NijifanAdapter,
    OppaisanAdapter,
    SengiribestAdapter,
    SexuadAdapter,
    YaruoAdapter,
)
from eromatome_dl.sites.moeimg import MoeimgAdapter
from eromatome_dl.sites.pashalism import PashalismAdapter


ADAPTERS: tuple[SiteAdapter, ...] = (
    MoeimgAdapter(),
    PashalismAdapter(),
    KimootokoAdapter(),
    IchinukeAdapter(),
    EroconAdapter(),
    EromanidcAdapter(),
    ErogazouGalleryAdapter(),
    ErokanAdapter(),
    EropuruAdapter(),
    HnaladyAdapter(),
    NijifanAdapter(),
    BeppinGirlAdapter(),
    DebusenAdapter(),
    EroGazouAdapter(),
    BakufuAdapter(),
    SengiribestAdapter(),
    ItaDoAdapter(),
    LoveliveforeverAdapter(),
    MegamichAdapter(),
    NijiPinkAdapter(),
    NijimoeEroGazouAdapter(),
    KyarabetsuNijieroAdapter(),
    SexuadAdapter(),
    FemmedollAdapter(),
    HentaiWitchAdapter(),
    AdamanEroAdapter(),
    EchiechiGazouAdapter(),
    OppaisanAdapter(),
    LesKokoAdapter(),
    YaruoAdapter(),
    GenericMatomeAdapter(),
)


def adapter_for_url(url: str) -> SiteAdapter | None:
    for adapter in ADAPTERS:
        if adapter.supports(url):
            return adapter
    return None

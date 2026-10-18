"""What a CPU's model name tells us that the CPU doesn't report itself:
microarchitecture, process, launch year and, for exact models, cache sizes.

The table covers the families nodes are likely to run, not every part. A model
it doesn't know, or one whose name several generations share (most mobile
parts), gets no lookup rather than a guess. Years are the launch year of the
model when the table lists it exactly, else of its generation's desktop parts.
Cache sizes are per chip, so a VM gets the size of the whole host chip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CpuSpec:
    gen: str                     # microarchitecture, e.g. "Zen 2"
    codename: str | None = None  # die or product line, e.g. "Matisse"
    process: str | None = None   # e.g. "7 nm", "Intel 7"
    year: int | None = None
    l2_kb: int | None = None     # per chip; exact models only
    l3_kb: int | None = None
    virtual: bool = False        # a hypervisor's CPU model, not a real chip


def normalize(model: str) -> str:
    s = re.sub(r"\((R|TM|tm)\)", "", model or "")
    return re.sub(r"\s+", " ", s).strip()


# Exact models: (pattern, spec). Matched before the family rules.
EXACT: list[tuple[str, CpuSpec]] = [
    (r"\bCore2 Quad (CPU )?Q6600\b", CpuSpec("Core", "Kentsfield", "65 nm", 2007, 8192, None)),
    (r"\bPentium (CPU )?G6950\b", CpuSpec("Westmere", "Clarkdale", "32 nm", 2010, 512, 3072)),
    (r"\bi3-8121U\b", CpuSpec("Palm Cove", "Cannon Lake", "10 nm", 2018)),
    (r"\bRyzen 5 3600X?\b", CpuSpec("Zen 2", "Matisse", "7 nm", 2019, 3072, 32768)),
    (r"\bRyzen 7 3700X\b", CpuSpec("Zen 2", "Matisse", "7 nm", 2019, 4096, 32768)),
    (r"\bRyzen 9 3900X\b", CpuSpec("Zen 2", "Matisse", "7 nm", 2019, 6144, 65536)),
    (r"\bRyzen 9 3950X\b", CpuSpec("Zen 2", "Matisse", "7 nm", 2019, 8192, 65536)),
    (r"\bRyzen 5 5600X\b", CpuSpec("Zen 3", "Vermeer", "7 nm", 2020, 3072, 32768)),
    (r"\bRyzen 7 5800X\b", CpuSpec("Zen 3", "Vermeer", "7 nm", 2020, 4096, 32768)),
    (r"\bRyzen 7 5800XT\b", CpuSpec("Zen 3", "Vermeer", "7 nm", 2024, 4096, 32768)),
    (r"\bRyzen 7 5800X3D\b", CpuSpec("Zen 3", "Vermeer", "7 nm", 2022, 4096, 98304)),
    (r"\bRyzen 9 5900X\b", CpuSpec("Zen 3", "Vermeer", "7 nm", 2020, 6144, 65536)),
    (r"\bRyzen 9 5900XT\b", CpuSpec("Zen 3", "Vermeer", "7 nm", 2024, 8192, 65536)),
    (r"\bRyzen 9 5950X\b", CpuSpec("Zen 3", "Vermeer", "7 nm", 2020, 8192, 65536)),
    (r"\bXeon Silver 4116\b", CpuSpec("Skylake", "Skylake-SP", "14 nm", 2017, 12288, 16896)),
    (r"\bXeon (CPU )?E5-2690 v4\b", CpuSpec("Broadwell", "Broadwell-EP", "14 nm", 2016, 3584, 35840)),
    (r"\bEPYC 7551P?\b", CpuSpec("Zen", "Naples", "14 nm", 2017, 16384, 65536)),
]

# A hypervisor's named CPU model ("Intel Core Processor (Haswell, no TSX)",
# "AMD EPYC-Rome Processor", "QEMU Virtual CPU"): the feature set it exposes,
# not the host's chip.
_VIRTUAL = [
    re.compile(r"^(Intel|AMD) (Core|Xeon|EPYC|Opteron)[- ]?(?P<feat>[A-Za-z]*) Processor\b ?(\((?P<feat2>[A-Za-z]+)[^)]*\))?"),
    re.compile(r"^(QEMU Virtual CPU|Common KVM processor|Common 32-bit KVM processor)"),
]

_INTEL_CORE = {  # generation -> (microarchitecture, process, desktop launch year)
    2: ("Sandy Bridge", "32 nm", 2011), 3: ("Ivy Bridge", "22 nm", 2012), 4: ("Haswell", "22 nm", 2013),
    5: ("Broadwell", "14 nm", 2015), 6: ("Skylake", "14 nm", 2015), 7: ("Kaby Lake", "14 nm", 2017),
    8: ("Coffee Lake", "14 nm", 2017), 9: ("Coffee Lake Refresh", "14 nm", 2018),
    10: ("Comet Lake", "14 nm", 2020), 11: ("Rocket Lake", "14 nm", 2021),
    12: ("Alder Lake", "Intel 7", 2021), 13: ("Raptor Lake", "Intel 7", 2022),
    14: ("Raptor Lake Refresh", "Intel 7", 2023),
}
_DESKTOP_SUFFIX = {"", "K", "KF", "F", "T", "S", "X", "XE", "KS", "P"}
_XEON_E3 = {1: ("Sandy Bridge", "32 nm", 2011), 2: ("Ivy Bridge", "22 nm", 2012), 3: ("Haswell", "22 nm", 2013),
            4: ("Broadwell", "14 nm", 2015), 5: ("Skylake", "14 nm", 2015), 6: ("Kaby Lake", "14 nm", 2017)}
_XEON_E5 = {1: ("Sandy Bridge", "Sandy Bridge-EP", "32 nm", 2012), 2: ("Ivy Bridge", "Ivy Bridge-EP", "22 nm", 2013),
            3: ("Haswell", "Haswell-EP", "22 nm", 2014), 4: ("Broadwell", "Broadwell-EP", "14 nm", 2016)}
_XEON_SP = {1: ("Skylake", "Skylake-SP", "14 nm", 2017), 2: ("Cascade Lake", "Cascade Lake-SP", "14 nm", 2019),
            3: ("Sunny Cove", "Ice Lake-SP", "10 nm", 2021), 4: ("Golden Cove", "Sapphire Rapids", "Intel 7", 2023),
            5: ("Raptor Cove", "Emerald Rapids", "Intel 7", 2023)}
_RYZEN_DESKTOP = {1: ("Zen", "Summit Ridge", "14 nm", 2017), 2: ("Zen+", "Pinnacle Ridge", "12 nm", 2018),
                  3: ("Zen 2", "Matisse", "7 nm", 2019), 5: ("Zen 3", "Vermeer", "7 nm", 2020),
                  7: ("Zen 4", "Raphael", "5 nm", 2022), 9: ("Zen 5", "Granite Ridge", "4 nm", 2024)}
_RYZEN_APU = {2: ("Zen", "Raven Ridge", "14 nm", 2018), 3: ("Zen+", "Picasso", "12 nm", 2019),
              4: ("Zen 2", "Renoir", "7 nm", 2020), 5: ("Zen 3", "Cezanne", "7 nm", 2021),
              8: ("Zen 4", "Phoenix", "4 nm", 2024)}
_THREADRIPPER = {1: ("Zen", "Whitehaven", "14 nm", 2017), 2: ("Zen+", "Colfax", "12 nm", 2018),
                 3: ("Zen 2", "Castle Peak", "7 nm", 2019), 5: ("Zen 3", "Chagall", "7 nm", 2022),
                 7: ("Zen 4", "Storm Peak", "5 nm", 2023)}
_EPYC = {"71": ("Zen", "Naples", "14 nm", 2017), "72": ("Zen 2", "Rome", "7 nm", 2019),
         "73": ("Zen 3", "Milan", "7 nm", 2021), "94": ("Zen 4", "Genoa", "5 nm", 2022),
         "84": ("Zen 4c", "Siena", "5 nm", 2023), "95": ("Zen 5", "Turin", None, 2024)}
_PI = [(r"Raspberry Pi 5\b", CpuSpec("Cortex-A76", "BCM2712", "16 nm", 2023)),
       (r"Raspberry Pi 4\b", CpuSpec("Cortex-A72", "BCM2711", "28 nm", 2019)),
       (r"Raspberry Pi 3 Model B Plus\b", CpuSpec("Cortex-A53", "BCM2837B0", "40 nm", 2018)),
       (r"Raspberry Pi 3 Model B\b", CpuSpec("Cortex-A53", "BCM2837", "40 nm", 2016))]


def _family(s: str) -> CpuSpec | None:
    m = re.search(r"\bCore2 (Duo|Quad|Extreme) (CPU )?([EQX])(\d)\d{3}\b", s)
    if m:
        series = m.group(3) + m.group(4)
        return {"Q6": CpuSpec("Core", "Kentsfield", "65 nm", 2007), "Q8": CpuSpec("Penryn", "Yorkfield", "45 nm", 2008),
                "Q9": CpuSpec("Penryn", "Yorkfield", "45 nm", 2008), "E4": CpuSpec("Core", "Allendale", "65 nm", 2007),
                "E6": CpuSpec("Core", "Conroe", "65 nm", 2006), "E7": CpuSpec("Penryn", "Wolfdale", "45 nm", 2008),
                "E8": CpuSpec("Penryn", "Wolfdale", "45 nm", 2008)}.get(series)
    if re.search(r"\bPentium (CPU )?G69\d\d\b", s):
        return CpuSpec("Westmere", "Clarkdale", "32 nm", 2010)
    m = re.search(r"\bPentium Gold G([567])\d{3}\b", s)
    if m:
        return {"5": CpuSpec("Coffee Lake", None, "14 nm", 2018), "6": CpuSpec("Comet Lake", None, "14 nm", 2020),
                "7": CpuSpec("Alder Lake", None, "Intel 7", 2022)}[m.group(1)]
    m = re.search(r"\bCore i[3579]-(\d{4,5})([A-Z]*)\b", s)
    if m:
        num, suffix = m.group(1), m.group(2)
        gen = int(num[:2]) if num[0] == "1" else int(num[0])
        if len(num) == 4 and num[0] == "1":       # 10th-14th gen mobile: 1035G1, 1135G7, 1255U
            if suffix.startswith("G") and gen == 10:
                return CpuSpec("Sunny Cove", "Ice Lake", "10 nm", 2019)
            if suffix.startswith("G") and gen == 11:
                return CpuSpec("Willow Cove", "Tiger Lake", "10 nm", 2020)
            if gen in (12, 13):
                g = _INTEL_CORE[gen]
                return CpuSpec(g[0], None, g[1], None)
            return None
        if gen not in _INTEL_CORE:
            return None
        g = _INTEL_CORE[gen]
        if suffix in _DESKTOP_SUFFIX:
            return CpuSpec(g[0], None, g[1], g[2])
        if gen == 11 and suffix.startswith("H"):
            return CpuSpec("Willow Cove", "Tiger Lake-H", "10 nm", 2021)
        if 6 <= gen <= 10:                       # mobile: Skylake-derived, all 14 nm
            return CpuSpec("Skylake family", None, "14 nm", None)
        if gen <= 5 or gen >= 12:
            return CpuSpec(g[0], None, g[1], None)
        return None
    m = re.search(r"\bCore Ultra [579] ([12])\d{2}([A-Z]*)\b", s)
    if m:
        if m.group(1) == "1":
            return CpuSpec("Redwood Cove", "Meteor Lake", "Intel 4", 2023)
        if m.group(2) in ("V",):
            return CpuSpec("Lion Cove", "Lunar Lake", "TSMC N3B", 2024)
        if m.group(2) in ("K", "KF", "F", "", "T"):
            return CpuSpec("Lion Cove", "Arrow Lake", "TSMC N3B", 2024)
        return None
    m = re.search(r"\bXeon (CPU )?E3-12\d\d[A-Z]? ?(v(\d))?\b", s)
    if m:
        g = _XEON_E3.get(int(m.group(3) or 1))
        return CpuSpec(g[0], None, g[1], g[2]) if g else None
    m = re.search(r"\bXeon (CPU )?E5-[124]\d{3}[A-Z]? ?(v(\d))?\b", s)
    if m:
        g = _XEON_E5.get(int(m.group(3) or 1))
        return CpuSpec(*g) if g else None
    m = re.search(r"\bXeon (Bronze|Silver|Gold|Platinum) \d(\d)\d{2}([A-Z]*)\b", s)
    if m:
        gen = int(m.group(2))
        if gen == 3 and m.group(3).startswith("H"):
            return CpuSpec("Cascade Lake", "Cooper Lake", "14 nm", 2020)
        g = _XEON_SP.get(gen)
        return CpuSpec(*g) if g else None
    m = re.search(r"\bRyzen Threadripper (PRO )?(\d)9\d\d(WX|X)?\b", s)
    if m:
        g = _THREADRIPPER.get(int(m.group(2)))
        return CpuSpec(*g) if g else None
    m = re.search(r"\bRyzen [3579] (PRO )?(\d)\d{3}([A-Z0-9]*)\b", s)
    if m:
        series, suffix = int(m.group(2)), m.group(3)
        if suffix in ("", "X", "XT", "X3D"):
            g = _RYZEN_DESKTOP.get(series)
            # later XT / X3D parts of a series launched years after it
            return CpuSpec(g[0], g[1], g[2], g[3] if suffix in ("", "X") else None) if g else None
        if suffix in ("G", "GE"):
            g = _RYZEN_APU.get(series)
            return CpuSpec(*g) if g else None
        return None                               # mobile: generations mixed within a series
    m = re.search(r"\bEPYC (\d)\d\d(\d)[A-Z]*\b", s)
    if m:
        g = _EPYC.get(m.group(1) + m.group(2))
        return CpuSpec(*g) if g else None
    m = re.search(r"\bFX-(\d)(\d)\d\d\b", s)
    if m:
        return {"1": CpuSpec("Bulldozer", "Zambezi", "32 nm", 2011),
                "3": CpuSpec("Piledriver", "Vishera", "32 nm", 2012),
                "5": CpuSpec("Piledriver", "Vishera", "32 nm", 2013)}.get(m.group(2))
    for pat, spec in _PI:
        if re.search(pat, s):
            return spec
    return None


def lookup(model: str) -> CpuSpec | None:
    s = normalize(model)
    if not s:
        return None
    for rx in _VIRTUAL:
        m = rx.search(s)
        if m:
            feat = (m.groupdict().get("feat2") or m.groupdict().get("feat") or "").strip()
            return CpuSpec(f"virtual CPU ({feat} feature set)" if feat else "virtual CPU", virtual=True)
    for pat, spec in EXACT:
        if re.search(pat, s):
            return spec
    return _family(s)


# Instruction sets the sidecar checks, in display order, by architecture.
ISA_X86 = [("ssse3", "SSSE3"), ("sse4_1", "SSE4.1"), ("sse4_2", "SSE4.2"), ("aes", "AES-NI"), ("avx", "AVX"),
           ("avx2", "AVX2"), ("avx512f", "AVX-512"), ("sha_ni", "SHA"), ("bmi2", "BMI2"), ("adx", "ADX")]
ISA_ARM = [("asimd", "NEON"), ("aes", "AES"), ("sha2", "SHA2"), ("pmull", "PMULL")]
# what sidecars before 0.1.5 checked (they send no cpu_flags_checked)
OLD_CHECKED = "aes,avx,avx2,avx512f,sha_ni,bmi2,adx,asimd,sha2,pmull"


def isa(arch: str, flags: str, checked: str | None) -> list[dict]:
    """[{n: display name, on: bool}] for the flags the node's sidecar checked."""
    have = {f for f in str(flags or "").split(",") if f}
    tested = {f for f in str(checked or OLD_CHECKED).split(",") if f}
    table = ISA_ARM if str(arch or "").startswith(("aarch64", "arm")) else ISA_X86
    return [{"n": name, "on": key in have} for key, name in table if key in tested]


def parse_cache(value: str) -> dict[str, int]:
    """Sidecar `cpu_cache` ("l2=512K l2n=12 l3=16384K l3n=2") -> total bytes per level."""
    kv = dict(p.split("=", 1) for p in str(value or "").split() if "=" in p)
    out = {}
    for level in ("l2", "l3"):
        m = re.fullmatch(r"(\d+)([KMG]?)", kv.get(level, ""))
        n = kv.get(level + "n", "1")
        if m and n.isdigit():
            size = int(m.group(1)) * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[m.group(2)]
            out[level] = size * max(1, int(n))
    return out

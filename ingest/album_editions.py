"""Album-edition heuristics: which release of a song counts, and how long an
album really is.

Spotify hands us the same recording under several releases -- a pre-release
single, the album, a deluxe edition padded with live takes, a greatest-hits
compilation -- and the isrc is identical across them. Something has to pick one,
and the pick drives both the album a track is filed under and the N that
album-listen coverage divides by.

Two rules, both taken from how the listening actually looks:
  - Prefer the release the user plays as an album: real albums over singles,
    the album proper over a deluxe or a commentary version, then whichever has
    more plays.
  - An album's length is its *standard* edition. A deluxe that appends 20 live
    takes to 12 studio songs is a 12-track album with padding, not a 32-track
    album, and the padding is not part of a listen.

Shared by enrich_album_editions.py (backfill), ingest_history.py (don't
re-orphan new plays) and build_album_listens.py (N and pass membership), so the
three can't drift.
"""
import re

# Fewer tracks than this and a release is a single, not an album worth counting
# listens for. 3 keeps genuine short EPs (Beach Bunny's 3-track "Crybaby") and
# drops 1-2 track singles, which is the only place the N=1 coverage blowup lives.
ALBUM_MIN_TRACKS = 3

# A standard edition can't plausibly be shorter than this, so a "core" count
# below it means the variant heuristic misfired -- on a live album, say, where
# every title is a live take -- and we keep total_tracks instead.
MIN_CORE_TRACKS = 3

# Nor can it be a sliver of the release. Below this the "standard edition" being
# claimed isn't one: a B-sides-and-sessions box (Doolittle 25, 35 tracks) opens
# with a few unmarked titles and is otherwise alternate takes, and shrinking N
# to those would make it trivially completable. Padding runs deep on real
# deluxes -- Who's Next is 9 of 29 -- so the bar has to sit well under a third.
MIN_CORE_RATIO = 0.2

# Bounds on the sibling rule (standard_edition_tracks), which trusts another
# release of the same record over anything the tracklist says. Box-set inflation
# only happens at real length, and only when the other edition is much shorter;
# below these an EP and an album that happen to share a title would collide.
BOX_SET_MIN_TRACKS = 16
BOX_SET_MIN_RATIO = 1.5

# How pure the tail has to be to read as appended padding rather than part of
# the record. Not 1.0, because bonus discs carry the odd outtake whose title
# gives nothing away -- Rumours' ends with "Keep Me There - With Vocal" among 24
# marked live takes and demos. Set high enough that a compilation, where
# alternate takes run all the way through, still fails.
VARIANT_TAIL_RATIO = 0.8

# Editions that pad a standard album. Deliberately excludes "remaster": a
# remastered reissue is the standard album, same tracklist. These words are
# never an album title on their own, so they count anywhere in the name.
DELUXE_RE = re.compile(
    r"""\b(
          deluxe | expanded | anniversary | legacy \s+ edition
        | special \s+ edition | collector'?s | complete \s+ edition
        | bonus \s+ track \s+ version
    )\b""",
    re.X | re.I,
)

# Alternate *formats* of a record -- the album with the band talking over it,
# the backing tracks, the pitch-shifted reissue. Not padding, but not the
# release anyone means by the album's name either.
#
# Only counted inside a qualifier, unlike DELUXE_RE: these are perfectly good
# album titles on their own. Adrianne Lenker's "instrumentals", Molly Sarlé's
# "Karaoke Angel" and Jodi's "Karaoke" are records, not formats of one.
ALT_FORMAT_RE = re.compile(
    r"""\b(
          commentary | track \s+ by \s+ track
        | karaoke | instrumentals? | a[\s-]?cappella
        | sped[\s-]?up | slowed | backing \s+ track
        | live                       # "hopeless fountain kingdom (Live From Webster Hall)"
    )\b""",
    re.X | re.I,
)

# Qualifier segments that mark a track as an alternate take rather than the
# album cut. Matched only against the qualifier part of a title (see
# _qualifiers), never the whole thing -- otherwise "Live Forever" and
# "Demolition" get caught.
VARIANT_RE = re.compile(
    r"""\b(
          live | acoustic | unplugged | demo | rehearsal | out-?take
        | instrumental | a[\s-]?cappella | karaoke | commentary
        | remix | re-mixed | reworked?
        | alternat(?:e|ive) \s+ (?:take|version|mix|cut)
        | (?:early|rough|work(?:ing)?) \s+ (?:take|version|mix)
        | take \s* \d+
        | bonus \s+ track
        | radio \s+ edit | single \s+ version | extended \s+ (?:version|mix)
        | session
    )\b""",
    re.X | re.I,
)

# A qualifier that only credits a guest or names a soundtrack says nothing about
# which take this is, so it never marks a variant.
CREDIT_QUALIFIER_RE = re.compile(r"^\s*(?:feat\.?|featuring|with|from)\b", re.I)

# Remaster and year tokens are noise, dropped before the variant test rather
# than vetoing it. They travel *with* the interesting word often enough that
# vetoing loses real padding: Rumours' bonus disc is full of "Live 1977; 2013
# Remaster" and "Demo; 2013 Remaster", which a veto reads as plain album cuts.
REMASTER_NOISE_RE = re.compile(r"\b(?:19|20)\d{2}\b | \bre-?master(?:ed)?\b", re.X | re.I)

_PAREN_RE = re.compile(r"[(\[]([^)\]]*)[)\]]")
# Spotify's other convention: "Song - Live at Wembley", "Song - 2011 Remaster".
_DASH_RE = re.compile(r"\s[-–—]\s(.+)$")


def _qualifiers(title: str) -> list[str]:
    """The parenthetical and dash-suffix segments of a track title."""
    if not title:
        return []
    quals = _PAREN_RE.findall(title)
    dash = _DASH_RE.search(_PAREN_RE.sub("", title))
    if dash:
        quals.append(dash.group(1))
    return quals


def is_variant_title(title: str) -> bool:
    """True for a live/demo/remix/instrumental take of a song."""
    return any(
        not CREDIT_QUALIFIER_RE.match(q) and VARIANT_RE.search(REMASTER_NOISE_RE.sub("", q))
        for q in _qualifiers(title)
    )


def is_secondary_edition(album_name: str) -> bool:
    """True for a release that isn't the album proper: a deluxe, or an
    alternate format like the commentary or karaoke version."""
    if not album_name:
        return False
    return bool(DELUXE_RE.search(album_name)) or any(
        ALT_FORMAT_RE.search(q) for q in _qualifiers(album_name))


_TRAILING_QUALIFIER_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]\s*$|\s[-–—]\s[^-–—]*$")


def base_album_name(album_name: str) -> str:
    """'Pet Sounds (Original Mono & Stereo Mix Versions)' -> 'pet sounds'.

    Strips every trailing qualifier, not just the ones DELUXE_RE knows: the
    interesting editions are named things like "(2018 Stereo Remaster)" and
    "(45th Anniversary / Super Deluxe Edition)", and enumerating that vocabulary
    is a losing game. Only used to match releases of the same record against
    each other, where a too-eager strip costs nothing -- the caller still
    requires the same artist and a large size gap.
    """
    if not album_name:
        return ""
    name = album_name
    while True:
        stripped = _TRAILING_QUALIFIER_RE.sub("", name).strip()
        if stripped == name or not stripped:
            break
        name = stripped
    return re.sub(r"\s+", " ", name).strip().lower()


def standard_edition_tracks(name: str, total_tracks: int | None,
                            siblings: list[tuple[str, int | None]]) -> int | None:
    """Length of the standard edition of this record, from another release of it.

    `siblings` is [(name, total_tracks)] for the same artist. A 65-track "45th
    Anniversary" of an 11-track album is a box set, and the 11-track row is
    usually sitting right there -- better evidence than anything the tracklist
    can tell us, and the only thing that works when the extra discs are mono
    remixes and unmarked outtakes rather than titles marked "Live".

    Restricted to big releases with a much smaller sibling, so that an EP and an
    album sharing a title can't shrink the album down to the EP.
    """
    if not total_tracks or total_tracks < BOX_SET_MIN_TRACKS:
        return None
    base = base_album_name(name)
    smaller = [t for n, t in siblings
               if t and ALBUM_MIN_TRACKS <= t <= total_tracks / BOX_SET_MIN_RATIO
               and base_album_name(n) == base]
    return min(smaller) if smaller else None


def split_edition(total_tracks: int | None,
                  tracks: list[tuple[str, str]]) -> tuple[int | None, list[str]]:
    """Split an album's tracklist into the standard edition and its padding.

    `tracks` is [(spotify_id, title)] in disc/track order. Returns
    (core_tracks, padding_spotify_ids); core_tracks is None for "not padded --
    use total_tracks".

    Padding is only recognised as a *trailing run* of alternate takes, and only
    when everything before it is an album cut. That shape is what a deluxe
    edition or bonus disc actually looks like (Lucinda Williams (Deluxe
    Edition): 12 studio songs, then 20 live ones), and requiring a clean split
    is what keeps the rule off compilations, where "- Single Version" is the
    canonical recording and appears throughout. Stripping those would shrink N
    on a real 20-track record and hand it listens it never earned.

    Needs the full tracklist -- a partial one would undercount N and inflate
    coverage, the exact failure this module exists to prevent -- so a short list
    returns None rather than a guess.
    """
    if not total_tracks or len(tracks) != total_tracks:
        return None, []

    flags = [is_variant_title(title) for _, title in tracks]
    if not any(flags) or all(flags):
        return None, []                  # nothing padded / a live album

    core = flags.index(True)             # the album ends where the extras start
    tail = flags[core:]
    if sum(tail) / len(tail) < VARIANT_TAIL_RATIO:
        return None, []                  # interleaved, so not an appended tail
    if core < MIN_CORE_TRACKS or core < MIN_CORE_RATIO * total_tracks:
        return None, []
    # Everything after the split is padding, including the stragglers no title
    # pattern catches -- Rumours' bonus disc ends with an outtake called
    # "Keep Me There - With Vocal", which is no more part of the album than the
    # demos around it.
    return core, [sid for sid, _ in tracks[core:]]


def edition_rank(name: str, album_type: str | None, core_tracks: int | None,
                 total_tracks: int | None, plays: int) -> tuple:
    """Sort key (descending = better) for choosing which release owns a track.

    Ordered the way the rules read: an album beats a single outright, an
    original beats a greatest-hits compilation, the album proper beats a deluxe
    or a commentary version, and only then does play count -- "the version I've
    listened to the most" -- break the tie, with the longer tracklist last as a
    stable fallback.

    Length, not album_type, draws the album/single line: Spotify files most EPs
    under album_type='single', so trusting the type would keep EPs from ever
    owning their own tracks.
    """
    n = core_tracks or total_tracks or 0
    return (
        n >= ALBUM_MIN_TRACKS,
        album_type != "compilation",
        not is_secondary_edition(name),
        plays,
        n,
    )


if __name__ == "__main__":
    # Self-check: `python album_editions.py`. These are the calls that decide
    # what N is, so the false positives matter as much as the hits -- "Live and
    # Let Die" and "Demolition Man" are album cuts, and a remaster is not a
    # variant of anything.
    VARIANTS = [
        ("Am I Too Blue (Eindhoven Live)", True),
        ("Side of the Road - Live at Kpfk", True),
        ("Motion Sickness (Acoustic)", True),
        ("Song - Demo", True),
        ("Song (Instrumental)", True),
        ("Not Strong Enough - Live from Electric Lady", True),
        # remaster tags ride along with the real marker on bonus discs
        ("Gold Dust Woman - Live 1977; 2013 Remaster", True),
        ("Planets of the Universe - Demo; 2013 Remaster", True),
        ("Dreams - Take 2", True),
        ("I Just Wanted to See You So Bad", False),
        ("Live and Let Die", False),
        ("Demolition Man", False),
        ("Alive", False),
        ("Bohemian Rhapsody - 2011 Remaster", False),
        ("Cloud 9 (feat. Tegan and Sara)", False),
    ]
    DELUXES = [
        ("Lucinda Williams (Deluxe Edition)", True),
        ("Sour (Bonus Track Version)", True),
        ("Sit Resist (Commentary Version)", True),
        ("The Carpenter(Commentary Version)", True),
        ("Close The Distance (With Commentary)", True),
        ("Half-Light (The Instrumentals)", True),
        ("Is This It", False),
        ("Blonde on Blonde (Remastered)", False),
        # real records, not formats of one -- the marker only counts as a
        # qualifier, so a bare title keeps its place
        ("Karaoke Angel", False),
        ("instrumentals", False),
        ("Music of the Night: Instrumental Piano", False),
    ]
    # 12 studio songs then 20 live ones: the shape core_tracks exists to find.
    LUCINDA = ([(f"s{i}", f"Song {i}") for i in range(12)]
               + [(f"v{i}", f"Song {i} (Eindhoven Live)") for i in range(20)])
    # A singles compilation: alternate takes throughout, so nothing to strip.
    PATSY = [(f"p{i}", f"Song {i} - Single Version" if i % 2 else f"Song {i}")
             for i in range(20)]
    # A bonus disc with one unmarked outtake in it still reads as padding.
    RUMOURS = ([(f"a{i}", f"Song {i} - 2004 Remaster") for i in range(12)]
               + [(f"b{i}", f"Song {i} - Live 1977") for i in range(24)]
               + [("x", "Keep Me There - With Vocal; 2013 Remaster")])

    SIBLINGS = [("Pet Sounds", 13), ("Pet Sounds (Mono)", 13),
                ("Some Other Record", 40)]

    fails = 0
    for label, args, want in [
        # a 27-track mono+stereo double, next to the 13-track album
        ("box set", ("Pet Sounds (Original Mono & Stereo Mix Versions)", 27, SIBLINGS), 13),
        # too short to be a box set, so the sibling is ignored
        ("normal album", ("Pet Sounds (Remastered)", 14, SIBLINGS), None),
        # sibling is not enough shorter to be a different edition
        ("close sizes", ("Pet Sounds (Deluxe)", 18, SIBLINGS), None),
        ("no sibling", ("Rumours (Super Deluxe)", 58, SIBLINGS), None),
    ]:
        got = standard_edition_tracks(*args)
        if got != want:
            fails += 1
            print(f"standard_edition_tracks({label}) = {got}, want {want}")
    for title, want in VARIANTS:
        if is_variant_title(title) != want:
            fails += 1
            print(f"is_variant_title({title!r}) != {want}")
    for name, want in DELUXES:
        if is_secondary_edition(name) != want:
            fails += 1
            print(f"is_secondary_edition({name!r}) != {want}")
    for label, (total, tracks), want in [
        ("padded deluxe", (32, LUCINDA), 12),
        ("mostly-marked tail", (37, RUMOURS), 12),
        ("compilation",   (20, PATSY),   None),
        ("clean album",   (11, [(f"t{i}", f"Song {i}") for i in range(11)]), None),
        ("partial fetch", (11, [(f"t{i}", f"Song {i}") for i in range(4)]),  None),
    ]:
        got, _ = split_edition(total, tracks)
        if got != want:
            fails += 1
            print(f"split_edition({label}) = {got}, want {want}")
    print(f"{'FAILED: ' + str(fails) if fails else 'ok'}")
    raise SystemExit(1 if fails else 0)

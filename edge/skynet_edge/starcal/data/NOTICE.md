# Bundled star catalog: source and licence

`bright_stars.csv.gz` (8920 stars, V <= 6.5, ~200 kB) is an adapted subset
of the **HYG database v4.0** by David Nash / astronexus
(<https://www.astronexus.com/hyg>, <https://codeberg.org/astronexus/hyg>),
downloaded from
<https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v40.csv.gz>
(SHA-256 of the downloaded file:
`8e3ff9e67445e558a759b117910850cff1b1d4d492f45f715c2ee2db3d869bac`).

* Licence: **Creative Commons Attribution-ShareAlike 4.0 International**
  (CC BY-SA 4.0, <https://creativecommons.org/licenses/by-sa/4.0/>), as stated
  in the HYG repository's LICENSE file for v4.0 and later.
* Changes made: kept only stars with V <= 6.5 (the Sun removed); kept the
  Hipparcos and HR numbers, J2000 RA/Dec (converted from hours to degrees),
  proper motions and V magnitude; dropped all other columns; set the proper
  motion to zero where HYG has a placeholder (|pm| > 9000 mas/yr); sorted by
  magnitude. The regeneration script is `../build_catalog.py`.
* This data file, as an adapted database, is distributed under the same
  CC BY-SA 4.0 licence. That applies to the data file only, not to the
  UAP Finder source code that reads it.

HYG itself compiles the Hipparcos, Yale Bright Star and Gliese catalogues;
positions and proper motions of these stars come from Hipparcos (ESA, 1997).
Credit: ESA (Hipparcos), Yale University Observatory (BSC5), and
astronexus.com (HYG).

Licence alternatives that were considered and rejected: the Yale Bright Star
Catalogue 5th ed. has no clear current licence (Debian ships it in
non-free); the ESA Hipparcos catalogue is distributed under CC BY-NC 3.0 IGO
(non-commercial). If the project needs a non-share-alike data source, the
loader (`catalog.py`) only needs a CSV with the same seven columns.

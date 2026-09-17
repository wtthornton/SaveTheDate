# Pictures on the guest site

Two kinds of thing live here: **photographs**, which are openly-licensed placeholders
taken from the design canvas, and **plates**, which are engraved line drawings made for
this site. None of the photographs is of Bill, Lisa, Port Aransas, or A Shore Thing.

## Photographs — all placeholders

| File | Subject | Photographer | License |
| --- | --- | --- | --- |
| `hero-couple.jpg` | A couple on the beach | Hisu Lee | CC0 |
| `gulf-sunset.jpg` | The Gulf at sunset | Anthony Delanoix | CC0 |
| `gulf-evening.jpg` | The Gulf at dusk | Linda Xu | CC0 |
| `pier-sunset.jpg` | A figure on a fishing pier | Claude Piché | CC0 |
| `golf-course.jpg` | A golf course | Viktor Kiryanov | CC0 |
| **`beach-fire.jpg`** | **A driftwood fire on the sand** | **Mike Dickison** | **CC BY 4.0** |

### `beach-fire.jpg` carries an obligation the others do not

CC BY **requires the credit to be visible to the reader**, not filed in a repository. It
is therefore named in a credit line at the foot of The Wedding page, which is the only
page it appears on. **If that image is removed, remove the credit line with it. If
another CC BY image is added, it goes in the same line.** Nothing else on the site
carries an attribution requirement.

## Plates — drawn for this site

Engraved line drawings in the site's own palette and ornament: the same double rule as
the hero frame, the same diamond as the section dividers. They exist because four
scheduled items had no photograph that could honestly stand in, and a grid of grey
rectangles reads as unfinished.

| File | Subject |
| --- | --- |
| `plate-a-shore-thing.svg` | A beach house raised on pilings above the dunes |
| `plate-dinner-in-town.svg` | A row of storefronts under strung lights |
| `plate-departure-breakfast.svg` | A coffee pot and two cups in the morning sun |
| `plate-bay-fishing.svg` | A small skiff on the bay at first light |

They are ours, so they carry no attribution requirement, and they are safe to keep even
once real photography arrives — a drawing beside a photograph reads as a deliberate
pairing in a way that a placeholder never does.

## Replace the hero first

`hero-couple.jpg` is **someone else's wedding**, and it is the first image a guest sees.
The design canvas flags it the same way. `plate-a-shore-thing.svg` is the other one to
revisit: Jason's house is yours to photograph, and a drawing is a better stand-in than a
stranger's house, but it is still a stand-in.

## Which picture goes with which item

Chosen in `app/templating.py` (`segment_image`), matched on keywords rather than a
database column, because a schedule is host-entered content and not a fixed vocabulary.
Anything unrecognised falls back to a plate, so a card can never render empty. A
fishing photograph beside golf copy would be worse than no photograph at all, which is
why the mapping is explicit and tested.

## Rules for anything added later

- Nothing from portaransas.org, Google Maps or Vacasa — all copyrighted, none
  republishable.
- CC BY and stricter need a visible credit. CC0 does not.
- Resize and strip metadata before committing: guests open this on cellular, and the
  originals here ran to 1.5MB before optimization.

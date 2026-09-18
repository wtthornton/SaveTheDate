# Photographs on the guest site

**Every one of these is a placeholder.** They are openly-licensed photographs, and none
of them is of Lisa, Bill, or A Shore Thing. Several *are* of Port Aransas and Mustang
Island, which is worth the search it took: a guest who knows the island will recognize
the ferry and the Tarpon Inn porch, and a generic beach would have told them nothing.

## Of the actual place

| File | Subject | Photographer | License |
| --- | --- | --- | --- |
| `porch-tarpon-inn.jpg` | The Tarpon Inn porch, Port Aransas | Gruenemann | CC BY 2.0 |
| `ferry-sunset.jpg` | The Port Aransas ferry at sunset | BlankBlankBlank | CC BY 2.0 |

## Generic, but on theme

| File | Subject | Photographer | License |
| --- | --- | --- | --- |
| `hero-couple.jpg` | A couple on the beach | Hisu Lee | CC0 |
| `gulf-sunset.jpg` | The Gulf at sunset | Anthony Delanoix | CC0 |
| `gulf-evening.jpg` | The Gulf at dusk | Linda Xu | CC0 |
| `pier-sunset.jpg` | A figure on a fishing pier | Claude Piché | CC0 |
| `golf-course.jpg` | A golf course | Viktor Kiryanov | CC0 |
| `beach-fire.jpg` | A driftwood fire on the sand | Mike Dickison | CC BY 4.0 |
| `dinner-table.jpg` | A long table laid by candlelight | Dennis Wong | CC BY 2.0 |
| `breakfast-coffee.jpg` | Coffee and a pastry | Helen.Yang | CC BY 2.0 |

## The five CC BY images carry an obligation the CC0 ones do not

CC BY **requires the credit to be visible to a reader**, not filed in a repository. The
names are listed in `PHOTO_CREDITS` in `app/templating.py` and rendered at the foot of
every guest page except the print view.

**Keep that list in step with this table.** If a CC BY photograph is swapped out, remove
its name; if one is added, add it. A credit for an image that is no longer shown is as
wrong as a missing one.

## Where each one is used

Most are matched to schedule items by `segment_image()` in `app/templating.py`. Three are
placed by hand:

| File | Where |
| --- | --- |
| `hero-couple.jpg` | The invitation hero |
| `gulf-sunset.jpg` / `gulf-evening.jpg` | Behind the save-the-date, desktop / phone |
| `beach-fire.jpg` | **Full-bleed behind the save-the-date card's type** |

`beach-fire.jpg` is **CC BY**, so the save-the-date now renders a credit line where it
used to render none — it had been built from CC0 Gulf photographs only. That is
`photo_credit_for()` working: it builds each page's credit from the filenames the
template actually names, so dropping a CC BY picture in makes the credit appear by
itself, and dropping a CC0 one in makes it disappear. Swap that picture and the line
follows it without anyone remembering to.

It sits under a 66-86% scrim, and the contrast floor is **4.5:1 against the darkest part
of the picture, not its average**. A replacement that is brighter needs a heavier scrim,
never smaller type.

## Two to replace first

`hero-couple.jpg` is **someone else's wedding** and is the first thing a guest sees.
`porch-tarpon-inn.jpg` stands where A Shore Thing should be — it is genuinely on the
island, and its alt text says plainly that it is the Tarpon Inn rather than implying it
is Jason's house, but it is still a stand-in. Jason's house is Bill's to photograph.

## Which photograph goes with which item

Decided in `app/templating.py` (`segment_image`), matched on keywords rather than a
database column, because a schedule is host-entered content and not a fixed vocabulary.
Anything unrecognized falls back to a Gulf sunset, so a card can never render empty.

The mapping is tested by name rather than by matching the filename — bay fishing is
illustrated by `pier-sunset.jpg`, an actual fishing pier, and a filename check called
that wrong. A fishing photograph beside golf copy would be worse than none at all.

## Rules for anything added later

- Nothing from portaransas.org, Google Maps or Vacasa — all copyrighted, none
  republishable.
- CC BY and stricter need a visible credit, and a line in `PHOTO_CREDITS`. CC0 does not.
- Resize and strip metadata before committing. Guests open this on cellular, and the
  originals ran past 1MB each before optimization.
- Openverse (`api.openverse.org`) is where these were found; searching for "Port
  Aransas" and "Mustang Island" directly turned up far better material than generic
  terms like "beach house", which returned Californian surfers.

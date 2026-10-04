# Photos for synthetic demo documents

Drop a photo here to have it printed onto the generated demo identity documents.

**The face on a persona's document must be the face that does that persona's liveness check.**
The face-match tool compares the two, so a stock photo, or one presenter's photo on another
presenter's persona, fails the match and sends the case to a reviewer.

## Requirements

- **Format: JPEG** (`.jpg` / `.jpeg`). PNG will not work — the PDF generator embeds the JPEG
  bytes directly using PDF's `DCTDecode` filter, which is what makes photo embedding possible
  with no image library dependency at all. PNG would need decoding + re-compression.
- Any reasonable size. Portrait orientation looks best (it renders into an ID-photo box), and a
  well-lit, front-facing photo matches most reliably.
- These are real faces. They are gitignored here; never commit or share them.

## Naming

Name the file after the persona whose document should carry it:

```
demo-us-01.jpg    -> demo-us-01-passport.jpg          (clean path)
demo-us-02.jpg    -> demo-us-02-passport.jpg          (possible match)
demo-us-03.jpg    -> demo-us-03-passport.jpg          (Act 3)
demo-us-04.jpg    -> demo-us-04-passport.jpg          (namesake)
demo-uk-01.jpg    -> demo-uk-01-drivers_license.jpg
demo-au-01.jpg    -> demo-au-01-national_id.jpg
demo-jp-01.jpg    -> demo-jp-01-passport.jpg
```

Or drop a single `default.jpg` to use the same photo on every document — right when one presenter
runs every persona.

## Then regenerate

```
make demo-assets
```

Documents with no matching photo are generated with an empty portrait frame — a missing photo is
not an error for the generator, but it is a face-match failure on stage.

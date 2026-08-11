# Photos for synthetic demo documents

Drop a photo here to have it printed onto the generated demo identity documents.

## Requirements

- **Format: JPEG** (`.jpg` / `.jpeg`). PNG will not work — the PDF generator embeds the JPEG
  bytes directly using PDF's `DCTDecode` filter, which is what makes photo embedding possible
  with no image library dependency at all. PNG would need decoding + re-compression.
- Any reasonable size. Portrait orientation looks best (it renders into an ID-photo box).

## Naming

Name the file after the persona whose document should carry it:

```
demo-us-01.jpg    -> printed on demo-us-01-passport.pdf
demo-uk-01.jpg    -> printed on demo-uk-01-drivers_license.pdf
demo-au-01.jpg    -> printed on demo-au-01-national_id.pdf
demo-jp-01.jpg    -> printed on demo-jp-01-passport.pdf
```

Or drop a single `default.jpg` to use the same photo on all four documents.

## Then regenerate

```
make demo-assets
```

Documents with no matching photo are generated exactly as before (text-only) — a missing photo
is not an error.

---
name: edit-twin-captions
description: Caption the project open in Final Cut Pro with the Edit Twin MCP, in the user's own caption styles and fonts — one word at a time for reels/shorts, readable subtitle lines for long-form videos — and set up or change those styles.
---

# Captions with Edit Twin

Use the `edit-twin` MCP tools. Captions are Final Cut Pro titles placed from the transcript, timed to each word on the current edit. The user's original project is never modified: the result is imported as a NEW project.

## 1. Get the project and the user's caption styles
1. `get_style`: look at `caption_styles`, `caption_defaults` (which style is used for `short` and `long` videos) and `fonts` (fonts the user works with, and what for).
2. If the project isn't open yet: `fcp_export_project` (or `find_fcpxml_files` + `open_timeline`), then `get_timeline`.
3. Decide the format unless the user said: **short** (vertical, or under ~3 minutes) or **long** (landscape, longer). Tell the user which in one line. If the user is also rough-cutting, caption AFTER the cut, since captions follow the timeline as it is now.

## 2. If there's no saved style for this format, set one up first
Ask the user how they like their captions, or offer the two ways:
- **Copy a title they styled by hand (most exact).** Ask them to add one title in Final Cut, style it (font, size, colour, position, any animation) and tell you. Re-export (`fcp_export_project`, `open_timeline`), find the title's ref in `get_timeline` (connected items with `"type": "title"`), then `save_caption_style` with `from_title` = that ref, `mode` (`word` for short, `line` for long) and `default_for`.
- **Describe it.** Turn the font they name into the installed name with `list_fonts` (query = part of the name). If it isn't installed, say so and stop; don't substitute a font. Then `save_caption_style` with `mode`, `font`, `face`, `color`, `position` (`top`, `upper`, `middle`, `lower`, `bottom`), `case` (`lower` if they write in lowercase), `punctuation` (`strip` is usual for one-word captions), and `default_for`.
  - Short form: `mode` `word`, `max_words` 1 (2–3 for "a few words at a time").
  - Long form: `mode` `line`, `max_lines` 1 (2 for denser subtitles). Lines are measured in their font so they fit the frame (`max_width`, default 0.8 of the width) and stay comfortable to read (42 characters per line by default).
Saving a style also remembers its font as one of the user's fonts, so next time just use the default.

When the user names a font or style in passing ("do the bootzy captions"), match it against `get_style` `fonts` and `caption_styles` first.

## 3. Check the words, then add
1. `add_captions` with `dry_run=true` (and the style if the user named one; `vocabulary` for names and jargon). Read through the captions for misheard words (names, slang, product names). Show the user any you'd fix, plus a sample of a few captions so they can see the line breaks.
2. `add_captions` with `fixes` for the corrections (e.g. `{"gorge": "gorg"}`). It replaces captions added before.
3. Single-caption tweaks afterwards: `edit_title` with the caption's ref.

## 4. Check and send
1. `validate_timeline`, then `send_to_final_cut` with the project name "<original name> (captions)" (or "(rough cut + captions)" after a rough cut).
2. Titles don't show in `preview_frame`/`render_preview`. After import, if the user has the new project open, `fcp_screenshot` to check size and placement, and tell them how to adjust (`save_caption_style` with `size` / `position`, then `add_captions` again).
3. For long-form YouTube uploads, offer `export_srt` too, so they can upload closed captions.
4. Summarise: style used, number of captions, any fixes applied, where the SRT is.

## Rules
- Never guess a font that isn't installed, and never hardcode one: fonts come from the user's saved styles or what they name.
- Don't change caption styles the user didn't ask to change; to try a variation, save it under a new name.
- Never edit their original project; always send a new one.

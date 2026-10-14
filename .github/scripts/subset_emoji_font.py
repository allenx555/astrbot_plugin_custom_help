"""Build the offline menu icon font; requires fonttools==4.60.1."""

import argparse
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

# Also keep common custom-menu symbols; arbitrary emoji need a full user font.
COMMON_ICONS = "✅❌⚠️ℹ️☑️⭐❤️😀😄😊😂🎉👍👎📌📖📁📂📷🖼️🔍🔎🔧⚙️🔒🔓🔗🛠️🧩⌛⏳⚡📋💡🤖📦🚀📝❓"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--template", type=Path, default=Path("templates/base.typ"))
    args = parser.parse_args()
    font = TTFont(args.source)
    text = args.template.read_text(encoding="utf-8") + COMMON_ICONS
    options = subset.Options()
    # Preserve metadata and license; give the subset a distinct family so a
    # user's complete Noto font remains independently selectable.
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.name_legacy = True
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes={ord(char) for char in text})
    subsetter.subset(font)
    names = {
        1: "Custom Help Emoji",
        3: "CustomHelpEmoji-Regular-1",
        4: "Custom Help Emoji",
        6: "CustomHelpEmoji",
        16: "Custom Help Emoji",
    }
    for entry in font["name"].names:
        if entry.nameID in names:
            entry.string = names[entry.nameID].encode(entry.getEncoding())
    font.save(args.output)
    font.close()
    print(f"Menu icon font: {args.output.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()

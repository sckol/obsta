ObjC.import('AppKit');

function run([destination, emoji]) {
    const size = 256;
    const image = $.NSImage.alloc.initWithSize($.NSMakeSize(size, size));
    const attributes = $.NSDictionary.dictionaryWithObjectForKey(
        $.NSFont.fontWithNameSize('Apple Color Emoji', 180), $.NSFontAttributeName
    );
    const text = $.NSString.stringWithString(emoji);
    const bounds = text.sizeWithAttributes(attributes);
    image.lockFocus;
    text.drawAtPointWithAttributes(
        $.NSMakePoint((size - bounds.width) / 2, (size - bounds.height) / 2), attributes
    );
    image.unlockFocus;
    const bitmap = $.NSBitmapImageRep.imageRepWithData(image.TIFFRepresentation);
    const png = bitmap.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $.NSDictionary.dictionary);
    if (!png.writeToFileAtomically(destination, true)) {
        throw new Error('Could not write the application icon');
    }
}

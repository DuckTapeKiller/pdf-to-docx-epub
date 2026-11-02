// The macOS spell checker, for scan_to_pdf.py.
// usage: spell_check LANGUAGE < words, one per line. Prints the words it rejects.
import AppKit

let language = CommandLine.arguments[1]
let checker = NSSpellChecker.shared
while let word = readLine() {
    let range = checker.checkSpelling(of: word, startingAt: 0, language: language, wrap: false,
                                      inSpellDocumentWithTag: 0, wordCount: nil)
    if range.location != NSNotFound {
        print(word)
    }
}

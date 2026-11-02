// Text recognition with Apple Vision, for scan_to_pdf.py (macOS only).
// usage: vision_ocr OUTDIR LANGUAGE IMAGE...
// Writes OUTDIR/<image stem>.jsonl: one JSON object per recognised line, with the box
// in pixels from the top-left corner.
import Foundation
import Vision
import CoreGraphics
import ImageIO

let out = CommandLine.arguments[1]
let language = CommandLine.arguments[2]
for path in CommandLine.arguments.dropFirst(3) {
    let url = URL(fileURLWithPath: path) as CFURL
    guard let source = CGImageSourceCreateWithURL(url, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { continue }
    let W = Double(image.width), H = Double(image.height)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = [language]
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    var lines: [String] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let row: [String: Any] = ["t": candidate.string, "c": candidate.confidence,
                                  "x": Int(box.minX * W), "y": Int((1 - box.maxY) * H),
                                  "w": Int(box.width * W), "h": Int(box.height * H)]
        lines.append(String(data: try JSONSerialization.data(withJSONObject: row, options: []), encoding: .utf8)!)
    }
    let stem = URL(fileURLWithPath: path).deletingPathExtension().lastPathComponent
    try lines.joined(separator: "\n").write(toFile: "\(out)/\(stem).jsonl", atomically: true, encoding: .utf8)
}

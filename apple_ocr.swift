import Foundation
import Vision
import ImageIO

// Fixed local CLI: file bytes never leave this process or the host.
guard CommandLine.arguments.count == 2 else { exit(2) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { exit(3) }
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true
if #available(macOS 13, *) { request.automaticallyDetectsLanguage = true }
do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let lines: [[String: Any]] = (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text": candidate.string, "confidence": Double(candidate.confidence),
                "box": ["x": Double(box.origin.x), "y": Double(box.origin.y),
                        "width": Double(box.width), "height": Double(box.height)]]
    }
    let payload: [String: Any] = ["status": "analyzed", "provider": "apple-vision",
        "text": lines.compactMap { $0["text"] as? String }.joined(separator: "\n"),
        "lines": lines, "coordinateSystem": "normalized-bottom-left", "localOnly": true]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
} catch { exit(4) }

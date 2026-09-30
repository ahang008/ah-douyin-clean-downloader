import Foundation
import Vision
import ImageIO

guard CommandLine.arguments.count == 2,
      let input = try? String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8) else {
    exit(2)
}

func emit(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
          let line = String(data: data, encoding: .utf8) else { return }
    print(line)
    fflush(stdout)
}

for line in input.split(separator: "\n") {
    guard let data = String(line).data(using: .utf8),
          let row = try? JSONSerialization.jsonObject(with: data) as? [String: String],
          let videoID = row["video_id"], let imagePath = row["image_path"] else { continue }
    let imageURL = URL(fileURLWithPath: imagePath)
    guard let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        emit(["video_id": videoID, "error": "image_decode_failed", "ocr_lines": []])
        continue
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true
    do {
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        let observations = (request.results ?? []).sorted {
            let a = $0.boundingBox
            let b = $1.boundingBox
            return abs(a.midY - b.midY) > 0.025 ? a.midY > b.midY : a.minX < b.minX
        }
        let lines: [[String: Any]] = observations.compactMap { observation in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            let box = observation.boundingBox
            return [
                "text": candidate.string,
                "confidence": Double(candidate.confidence),
                "bbox": [Double(box.minX), Double(box.minY), Double(box.width), Double(box.height)]
            ]
        }
        emit(["video_id": videoID, "ocr_lines": lines])
    } catch {
        emit(["video_id": videoID, "error": "request_failed", "ocr_lines": []])
    }
}

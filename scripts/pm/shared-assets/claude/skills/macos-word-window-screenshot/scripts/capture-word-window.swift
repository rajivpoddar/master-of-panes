import CoreGraphics
import Foundation
import ImageIO

func fail(_ message: String, code: Int32 = 1) -> Never {
    fputs("WORD_SCREENSHOT_FAILED: \(message)\n", stderr)
    exit(code)
}

guard CommandLine.arguments.count == 3 else {
    fail("usage: swift capture-word-window.swift <title-substring> <new-output.png>", code: 2)
}

let titleQuery = CommandLine.arguments[1]
let output = URL(fileURLWithPath: CommandLine.arguments[2])
guard !titleQuery.isEmpty else { fail("title substring must not be empty", code: 2) }
guard output.pathExtension.lowercased() == "png" else { fail("output must end in .png", code: 2) }
guard !FileManager.default.fileExists(atPath: output.path) else {
    fail("output already exists: \(output.path)", code: 2)
}
guard CGPreflightScreenCaptureAccess() else {
    fail("Screen Recording is not granted to the requesting terminal app", code: 3)
}

let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
let matches = windows.compactMap { window -> (CGWindowID, String)? in
    guard (window[kCGWindowOwnerName as String] as? String) == "Microsoft Word",
          (window[kCGWindowLayer as String] as? Int) == 0,
          let title = window[kCGWindowName as String] as? String,
          title.localizedCaseInsensitiveContains(titleQuery),
          let number = window[kCGWindowNumber as String] as? NSNumber else {
        return nil
    }
    return (number.uint32Value, title)
}

guard matches.count == 1, let match = matches.first else {
    let titles = windows.compactMap { window -> String? in
        guard (window[kCGWindowOwnerName as String] as? String) == "Microsoft Word",
              (window[kCGWindowLayer as String] as? Int) == 0 else { return nil }
        return window[kCGWindowName as String] as? String
    }.filter { !$0.isEmpty }
    fail("expected one Word window matching '\(titleQuery)'; found \(matches.count). Visible titles: \(titles)", code: 4)
}

let capture = Process()
capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
capture.arguments = ["-x", "-l", String(match.0), output.path]
do {
    try capture.run()
    capture.waitUntilExit()
} catch {
    fail("could not launch screencapture: \(error)", code: 5)
}
guard capture.terminationStatus == 0,
      let image = CGImageSourceCreateWithURL(output as CFURL, nil),
      let properties = CGImageSourceCopyPropertiesAtIndex(image, 0, nil) as? [String: Any],
      let width = properties[kCGImagePropertyPixelWidth as String] as? Int,
      let height = properties[kCGImagePropertyPixelHeight as String] as? Int,
      width > 100, height > 100 else {
    fail("capture failed or did not produce a valid Word-window PNG", code: 5)
}

print("WORD_SCREENSHOT_OK path=\(output.path) title=\(match.1) window_id=\(match.0) size=\(width)x\(height)")

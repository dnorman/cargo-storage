import Foundation
import AppKit
import Darwin

// This changes Cargo's configuration for NEW invocations, never a running build's path.
// The volume UUID, not its name, selects the external disk. No switchable symlink is used.
struct Settings: Codable {
    let volumeUUID: String
    let externalSubdirectory: String
    let internalRoot: String
    let cargoConfig: String
    let stateDirectory: String

    func validate() throws {
        guard UUID(uuidString: volumeUUID) != nil else {
            throw Failure.message("volumeUUID must be a volume UUID")
        }
        let components = externalSubdirectory.split(separator: "/", omittingEmptySubsequences: false)
        guard !components.isEmpty, components.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }),
              !externalSubdirectory.contains(":"), !externalSubdirectory.contains("{"),
              !externalSubdirectory.contains("}") else {
            throw Failure.message("externalSubdirectory must be a relative path without empty, dot, parent or template components")
        }
        for path in [externalSubdirectory, internalRoot, cargoConfig, stateDirectory] {
            guard !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
                throw Failure.message("Settings paths cannot contain control characters")
            }
        }
        for path in [internalRoot, cargoConfig, stateDirectory] {
            guard path.hasPrefix("/"), !path.contains(":"), !path.contains("{"), !path.contains("}") else {
                throw Failure.message("Settings paths must be absolute and cannot contain colons or Cargo template braces")
            }
        }
    }
}

struct Route: Codable, Equatable {
    let mode: String
    let root: String
    let volumePath: String?
    let reason: String
    var template: String { root + "/{workspace-path-hash}" }
}

struct RecordedState: Codable {
    let updatedAt: String
    let route: Route
}

enum Failure: Error, CustomStringConvertible {
    case message(String)
    var description: String {
        switch self { case .message(let text): return text }
    }
}

let fm = FileManager.default
let beginMarker = "# BEGIN cargo-storage managed build-dir"
let endMarker = "# END cargo-storage managed build-dir"

func jsonString(_ text: String) throws -> String {
    // JSON's escaping for these filesystem paths is also valid TOML basic-string syntax.
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.withoutEscapingSlashes]
    return String(data: try encoder.encode(text), encoding: .utf8)!
}

func log(_ message: String) {
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(message)\n"
    FileHandle.standardError.write(Data(line.utf8))
}

func checkedDirectory(_ path: String) throws {
    if fm.fileExists(atPath: path) {
        let attrs = try fm.attributesOfItem(atPath: path)
        guard attrs[.type] as? FileAttributeType == .typeDirectory else {
            throw Failure.message("Storage root is not a plain directory: \(path)")
        }
    } else {
        try fm.createDirectory(atPath: path, withIntermediateDirectories: true)
    }
    guard access(path, W_OK | X_OK) == 0 else {
        throw Failure.message("Storage root is not writable: \(path)")
    }
}

func selectRoute(_ settings: Settings) throws -> Route {
    let keys: Set<URLResourceKey> = [.volumeUUIDStringKey, .volumeIsReadOnlyKey]
    let volumes = fm.mountedVolumeURLs(includingResourceValuesForKeys: Array(keys), options: []) ?? []
    var reason = "External SSD is not mounted"
    for volume in volumes {
        guard let values = try? volume.resourceValues(forKeys: keys),
              values.volumeUUIDString?.caseInsensitiveCompare(settings.volumeUUID) == .orderedSame else { continue }
        if values.volumeIsReadOnly == true {
            reason = "External SSD is mounted read-only"
            break
        }
        let root = volume.appendingPathComponent(settings.externalSubdirectory, isDirectory: true).path
        if root.contains(":") || root.contains("{") || root.contains("}") {
            reason = "External mount path contains a path separator or Cargo template braces"
            break
        }
        do {
            try checkedDirectory(root)
            // Revalidate identity after creating the directory, closing the ordinary unmount race.
            let stillMounted = try URL(fileURLWithPath: root).resourceValues(forKeys: keys)
            guard stillMounted.volumeUUIDString?.caseInsensitiveCompare(settings.volumeUUID) == .orderedSame else {
                throw Failure.message("External volume changed during selection")
            }
            return Route(mode: "external", root: root, volumePath: volume.path, reason: "External SSD is mounted and writable")
        } catch {
            reason = "External SSD is unavailable: \(error)"
            break
        }
    }
    try checkedDirectory(settings.internalRoot)
    return Route(mode: "internal", root: settings.internalRoot, volumePath: nil, reason: reason)
}

func updatedConfig(_ original: String, template: String) throws -> String {
    let starts = original.components(separatedBy: beginMarker)
    let ends = original.components(separatedBy: endMarker)
    guard starts.count == 2, ends.count == 2,
          let start = original.range(of: beginMarker),
          let end = original.range(of: endMarker), start.upperBound < end.lowerBound else {
        throw Failure.message("Cargo configuration's managed markers are missing or duplicated; refusing to overwrite it")
    }
    // Only operate inside [build]; edits to unrelated settings are always preserved.
    let preceding = String(original[..<start.lowerBound])
    let section = preceding.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }
        .last { $0.hasPrefix("[") && !$0.hasPrefix("#") }
    guard section == "[build]" else {
        throw Failure.message("Managed setting is no longer in [build]; refusing to overwrite it")
    }
    let managed = original[start.upperBound..<end.lowerBound].trimmingCharacters(in: .whitespacesAndNewlines)
    guard managed.hasPrefix("build-dir = "), !managed.contains("\n") else {
        throw Failure.message("Managed block was edited; refusing to discard its contents")
    }
    let replacement = "\nbuild-dir = \(try jsonString(template))\n"
    return String(original[..<start.upperBound]) + replacement + String(original[end.lowerBound...])
}

func atomicConfigWrite(_ content: String, at path: String, expected: Data) throws {
    let url = URL(fileURLWithPath: path)
    let attrs = try fm.attributesOfItem(atPath: path)
    guard attrs[.type] as? FileAttributeType == .typeRegular else {
        throw Failure.message("Cargo configuration is not a regular file")
    }
    let temporary = url.deletingLastPathComponent().appendingPathComponent(".cargo-storage-\(UUID().uuidString).tmp")
    defer { try? fm.removeItem(at: temporary) }
    try Data(content.utf8).write(to: temporary, options: .withoutOverwriting)
    try fm.setAttributes([.posixPermissions: attrs[.posixPermissions] ?? 0o600], ofItemAtPath: temporary.path)
    guard try Data(contentsOf: url) == expected else {
        throw Failure.message("Cargo configuration changed concurrently; will retry")
    }
    guard rename(temporary.path, path) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
}

final class Router {
    let settings: Settings
    var lastRoute: Route?
    var lastError: String?
    var observers: [NSObjectProtocol] = []
    var timer: Timer?

    init(_ settings: Settings) { self.settings = settings }

    @discardableResult
    func reconcile() throws -> Route {
        try checkedDirectory(settings.stateDirectory)
        let lockPath = settings.stateDirectory + "/config.lock"
        let fd = open(lockPath, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw Failure.message("Cannot open configuration lock") }
        defer { close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { throw Failure.message("Cannot lock configuration") }
        defer { flock(fd, LOCK_UN) }

        let route = try selectRoute(settings)
        let url = URL(fileURLWithPath: settings.cargoConfig)
        let configAttributes = try fm.attributesOfItem(atPath: settings.cargoConfig)
        guard configAttributes[.type] as? FileAttributeType == .typeRegular else {
            throw Failure.message("Cargo configuration is not a regular file")
        }
        let original = try Data(contentsOf: url)
        guard let text = String(data: original, encoding: .utf8) else { throw Failure.message("Cargo config is not UTF-8") }
        let updated = try updatedConfig(text, template: route.template)
        if updated != text { try atomicConfigWrite(updated, at: settings.cargoConfig, expected: original) }
        if route != lastRoute || updated != text {
            let state = RecordedState(updatedAt: ISO8601DateFormatter().string(from: Date()), route: route)
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(state)
            let stateURL = URL(fileURLWithPath: settings.stateDirectory + "/state.json")
            // No disk writes on unchanged timer ticks, or unchanged one-shot invocations.
            let prior = (try? Data(contentsOf: stateURL)).flatMap { try? JSONDecoder().decode(RecordedState.self, from: $0) }
            if prior?.route != route || updated != text { try data.write(to: stateURL, options: .atomic) }
            log("\(route.mode): \(route.root) — \(route.reason)")
        }
        lastRoute = route
        lastError = nil
        return route
    }

    func tick() {
        do { try reconcile() }
        catch {
            let message = String(describing: error)
            if message != lastError { log("ERROR: \(message)"); lastError = message }
        }
    }

    func watch() {
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didMountNotification, NSWorkspace.didUnmountNotification,
                     NSWorkspace.didRenameVolumeNotification, NSWorkspace.didWakeNotification] {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in self?.tick() })
        }
        tick()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.run()
    }
}

func displayPath(_ path: String) -> String {
    let home = fm.homeDirectoryForCurrentUser.path
    return path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
}

func displayTime(_ timestamp: String) -> String {
    guard let date = ISO8601DateFormatter().date(from: timestamp) else { return timestamp }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US")
    formatter.timeZone = .current
    if Calendar.current.isDateInToday(date) {
        formatter.dateFormat = "'Today at' h:mm a z"
    } else if Calendar.current.isDateInYesterday(date) {
        formatter.dateFormat = "'Yesterday at' h:mm a z"
    } else {
        formatter.dateFormat = "MMM d, yyyy 'at' h:mm a z"
    }
    return formatter.string(from: date)
}

func helperStatus() -> String {
    let process = Process()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    process.arguments = ["print", "gui/\(getuid())/net.danielnorman.cargo-storage"]
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(data: data, encoding: .utf8) ?? ""
        if process.terminationStatus == 0 && text.contains("state = running") {
            return "Running · automatic switching enabled"
        }
        return process.terminationStatus == 0 ? "Loaded, but not running" : "Not loaded or unavailable"
    } catch {
        return "Could not check background helper"
    }
}

func diskInfo(_ identifier: String) -> [String: Any]? {
    let process = Process()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/sbin/diskutil")
    process.arguments = ["info", "-plist", identifier]
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 4, execute: timeout)
        defer { timeout.cancel() }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        return (try? PropertyListSerialization.propertyList(from: data, format: nil)) as? [String: Any]
    } catch {
        return nil
    }
}

func nvmeCounter(_ values: [String: Any], _ name: String) -> Double? {
    guard let low = values[name + "_0"] as? NSNumber else { return nil }
    let high = (values[name + "_1"] as? NSNumber)?.doubleValue ?? 0
    return low.doubleValue + high * pow(2, 64)
}

func byteCount(_ bytes: NSNumber) -> String {
    ByteCountFormatter.string(fromByteCount: bytes.int64Value, countStyle: .decimal)
}

func printDiskHealth(_ title: String, info: [String: Any]?) {
    print("  \(title)")
    guard let info else {
        print("    Health data unavailable\n")
        return
    }
    let smart = info["SMARTDeviceSpecificKeysMayVaryNotGuaranteed"] as? [String: Any] ?? [:]
    let smartStatus = info["SMARTStatus"] as? String
    var summary: [String] = []
    if smartStatus == "Verified" {
        summary.append("SMART verified")
    } else if let smartStatus, smartStatus != "Not Supported" {
        summary.append("SMART: \(smartStatus)")
    } else if smart.isEmpty {
        summary.append("SMART / wear data unavailable through this connection")
    }
    if let used = smart["PERCENTAGE_USED"] as? NSNumber {
        summary.append("\(used)% endurance used")
    }
    if let kelvin = smart["TEMPERATURE"] as? NSNumber, kelvin.doubleValue > 0 {
        summary.append(String(format: "%.0f°C", kelvin.doubleValue - 273.15))
    }
    if !summary.isEmpty { print("    " + summary.joined(separator: " · ")) }
    var counters: [String] = []
    if let units = nvmeCounter(smart, "DATA_UNITS_WRITTEN") {
        // NVMe SMART counts host data in units of 1,000 × 512 bytes.
        counters.append("\(byteCount(NSNumber(value: units * 512_000))) host writes")
    }
    if let errors = nvmeCounter(smart, "MEDIA_ERRORS") {
        counters.append(String(format: "%.0f media errors", errors))
    }
    if let spare = smart["AVAILABLE_SPARE"] as? NSNumber {
        counters.append("\(spare)% spare")
    }
    if !counters.isEmpty { print("    " + counters.joined(separator: " · ")) }
    if let free = info["APFSContainerFree"] as? NSNumber,
       let total = (info["APFSContainerSize"] ?? info["TotalSize"]) as? NSNumber {
        print("    \(byteCount(free)) free of \(byteCount(total))")
    }
    print("")
}

func printHealth(_ settings: Settings) {
    print("Disk health\n")
    printDiskHealth("Macintosh HD · internal SSD", info: diskInfo("/"))
    let volumes = fm.mountedVolumeURLs(includingResourceValuesForKeys: [.volumeUUIDStringKey], options: []) ?? []
    let external = volumes.first {
        (try? $0.resourceValues(forKeys: [.volumeUUIDStringKey]))?.volumeUUIDString?
            .caseInsensitiveCompare(settings.volumeUUID) == .orderedSame
    }
    if let external {
        printDiskHealth("\(external.lastPathComponent) · external SSD", info: diskInfo(settings.volumeUUID))
    } else {
        print("  External SSD\n    Not mounted or not visible to this process\n")
    }
}

func printStatus(_ settings: Settings) {
    let statePath = settings.stateDirectory + "/state.json"
    guard let state = try? JSONDecoder().decode(RecordedState.self, from: Data(contentsOf: URL(fileURLWithPath: statePath))) else {
        print("\nCargo storage — Not initialized\n")
        print("  The background helper has not recorded a storage destination.")
        print("  Helper      \(helperStatus())")
        print("  Config      \(displayPath(settings.cargoConfig))\n")
        return
    }
    let route = state.route
    let label = route.mode == "external" ? "External SSD" : "Internal SSD (fallback)"
    print("\nCargo storage — \(label)\n")
    if let volume = route.volumePath {
        print("  Drive       \(URL(fileURLWithPath: volume).lastPathComponent)")
    }
    print("  Status      \(route.reason)")
    print("  Helper      \(helperStatus())")
    print("  Changed     \(displayTime(state.updatedAt))")
    print("\n  Build intermediates")
    print("    \(displayPath(route.root))/")
    print("    Separate subfolder for each worktree")
    print("\n  Fallback    \(displayPath(settings.internalRoot))/")
    print("  Binaries    Each project's target directory")
    print("  Config      \(displayPath(settings.cargoConfig))\n")
    if let text = try? String(contentsOfFile: settings.cargoConfig, encoding: .utf8) {
        if (try? updatedConfig(text, template: route.template)) != text {
            print("  Note: Cargo config differs from this recorded route.\n")
        }
    }
    printHealth(settings)
}

do {
    let args = Array(CommandLine.arguments.dropFirst())
    let command = args.first ?? "status"
    if ["--help", "-h", "help"].contains(command) {
        print("Usage: cargo-storage [status|sync|watch] [settings.json]")
        print("Route new Cargo builds to a configured volume, with internal-storage fallback.")
        exit(0)
    }
    guard args.count <= 2, ["status", "sync", "watch"].contains(command) else {
        throw Failure.message("Usage: cargo-storage [status|sync|watch] [settings.json]")
    }
    let settingsPath = args.count > 1 ? args[1] : fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CargoStorage/settings.json").path
    let settings = try JSONDecoder().decode(Settings.self, from: Data(contentsOf: URL(fileURLWithPath: settingsPath)))
    try settings.validate()
    let router = Router(settings)
    switch command {
    case "watch": router.watch()
    case "sync":
        let route = try router.reconcile()
        print("\(route.mode): \(route.template)")
    case "status":
        printStatus(settings)
    default: throw Failure.message("Usage: cargo-storage [status|sync|watch] [settings.json]")
    }
} catch {
    log("ERROR: \(error)")
    exit(1)
}

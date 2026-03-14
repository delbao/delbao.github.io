import EventKit
import Foundation

struct EventPayload: Codable {
    let calendar_name: String
    let summary: String
    let start_epoch: Double
    let end_epoch: Double
    let description: String?
    let event_url: String?
    let attendees: [String]
}

func requestCalendarAccess(store: EKEventStore) -> Bool {
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false

    if #available(macOS 14.0, *) {
        store.requestFullAccessToEvents { accessGranted, _ in
            granted = accessGranted
            semaphore.signal()
        }
    } else {
        store.requestAccess(to: .event) { accessGranted, _ in
            granted = accessGranted
            semaphore.signal()
        }
    }

    _ = semaphore.wait(timeout: .now() + 10)
    return granted
}

func parseAllowedCalendars(_ text: String) -> Set<String> {
    let names = text
        .split(separator: ",")
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }
    return Set(names)
}

func main() -> Int32 {
    guard CommandLine.arguments.count >= 4 else {
        fputs("usage: calendar_eventkit_fetch.swift <start_epoch> <end_epoch> <calendar_names_csv>\n", stderr)
        return 64
    }

    guard
        let startEpoch = Double(CommandLine.arguments[1]),
        let endEpoch = Double(CommandLine.arguments[2])
    else {
        fputs("invalid epoch arguments\n", stderr)
        return 65
    }

    let allowedCalendars = parseAllowedCalendars(CommandLine.arguments[3])
    let store = EKEventStore()
    guard requestCalendarAccess(store: store) else {
        fputs("calendar access denied\n", stderr)
        return 66
    }

    let calendars = store
        .calendars(for: .event)
        .filter { allowedCalendars.contains($0.title) }

    let startDate = Date(timeIntervalSince1970: startEpoch)
    let endDate = Date(timeIntervalSince1970: endEpoch)
    let predicate = store.predicateForEvents(withStart: startDate, end: endDate, calendars: calendars)
    let events = store.events(matching: predicate)

    let payloads = events.compactMap { event -> EventPayload? in
        if event.isAllDay {
            return nil
        }
        let title = event.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if title.isEmpty {
            return nil
        }

        let attendees = (event.attendees ?? []).compactMap { participant -> String? in
            if let name = participant.name?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty {
                return name
            }
            let rawURL = participant.url.absoluteString
            if !rawURL.isEmpty {
                let normalized = rawURL.replacingOccurrences(of: "mailto:", with: "")
                if !normalized.isEmpty {
                    return normalized.removingPercentEncoding ?? normalized
                }
            }
            return nil
        }

        return EventPayload(
            calendar_name: event.calendar.title,
            summary: title,
            start_epoch: event.startDate.timeIntervalSince1970,
            end_epoch: event.endDate.timeIntervalSince1970,
            description: event.notes,
            event_url: event.url?.absoluteString,
            attendees: attendees
        )
    }

    let sortedPayloads = payloads.sorted {
        if $0.start_epoch != $1.start_epoch {
            return $0.start_epoch < $1.start_epoch
        }
        if $0.calendar_name != $1.calendar_name {
            return $0.calendar_name < $1.calendar_name
        }
        return $0.summary.localizedCaseInsensitiveCompare($1.summary) == .orderedAscending
    }

    do {
        let encoder = JSONEncoder()
        let output = try encoder.encode(sortedPayloads)
        FileHandle.standardOutput.write(output)
        return 0
    } catch {
        fputs("failed to encode event payloads: \(error)\n", stderr)
        return 67
    }
}

exit(main())

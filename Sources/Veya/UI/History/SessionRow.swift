import SwiftUI

struct SessionRow: View {
    let session: Session

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(session.title.isEmpty ? "Untitled Session" : session.title)
                    .font(.headline)
                HStack(spacing: 6) {
                    Text(session.sessionType.displayName)
                    if !session.company.isEmpty {
                        Text("· \(session.company)")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            statusBadge
            Text(session.createdAt.formatted(date: .abbreviated, time: .shortened))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
    }

    private var statusBadge: some View {
        Text(statusText)
            .font(.caption2.bold())
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(statusColor.opacity(0.18), in: Capsule())
            .foregroundStyle(statusColor)
    }

    private var statusText: String {
        switch session.status {
        case .notStarted: return "NOT STARTED"
        case .live: return "LIVE"
        case .ended: return "ENDED"
        }
    }

    private var statusColor: Color {
        switch session.status {
        case .notStarted: return .secondary
        case .live: return .green
        case .ended: return .blue
        }
    }
}

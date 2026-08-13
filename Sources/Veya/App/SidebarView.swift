import SwiftUI

/// Persistent left navigation — Dashboard, Create Session, Previous
/// Sessions, Memory, Settings, per the navigation/polish requirements.
/// Hidden during a Live Session (see `RootView.showsSidebar`).
struct SidebarView: View {
    @EnvironmentObject private var coordinator: AppCoordinator

    private struct Item: Identifiable {
        let id = UUID()
        let title: String
        let systemImage: String
        let route: AppCoordinator.Route
    }

    private let items: [Item] = [
        Item(title: "Dashboard", systemImage: "square.grid.2x2", route: .dashboard),
        Item(title: "Create Session", systemImage: "plus.circle", route: .createSession),
        Item(title: "Previous Sessions", systemImage: "clock.arrow.circlepath", route: .previousSessions),
        Item(title: "Memory", systemImage: "brain", route: .memory),
        Item(title: "Settings", systemImage: "gearshape", route: .settings)
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Veya")
                .font(.title2.bold())
                .padding(.horizontal, 16)
                .padding(.top, 20)
                .padding(.bottom, 12)

            ForEach(items) { item in
                SidebarRow(
                    title: item.title,
                    systemImage: item.systemImage,
                    isSelected: coordinator.route == item.route
                ) {
                    coordinator.route = item.route
                }
            }

            Spacer()
        }
        .frame(width: 200)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(.quaternary.opacity(0.15))
    }
}

private struct SidebarRow: View {
    let title: String
    let systemImage: String
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: systemImage)
                    .frame(width: 18)
                Text(title)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(isSelected ? Color.accentColor.opacity(0.18) : .clear, in: RoundedRectangle(cornerRadius: 6))
            .foregroundStyle(isSelected ? Color.accentColor : Color.primary)
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 8)
    }
}

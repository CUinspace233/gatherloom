import SwiftUI

/// The single source of spacing, radius, colour and type decisions for the app.
enum Theme {
    enum Space {
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
    }

    enum Radius {
        static let card: CGFloat = 20
        static let bubble: CGFloat = 16
    }
}

extension Color {
    /// The page background behind content. Slightly lifted from black in dark mode
    /// so the side drawer, which stays on the system background, reads as a lower layer.
    static let canvas = Color("Canvas")
    /// The same lift for grouped lists and forms, whose light value is the system grey.
    static let groupedCanvas = Color("GroupedCanvas")
    /// The one warm colour, reserved for status hints such as "changed" or "draft".
    static let warm = Color("Warm")
}

extension ShapeStyle where Self == Color {
    static var warm: Color { .warm }
    /// Text and icons on an accent-filled control. The accent is ink in light
    /// mode and near-white in dark mode, so the label takes the opposite.
    static var onAccent: Color { Color(.systemBackground) }
}

extension Font {
    /// The serif wordmark face. Latin only: iOS ships no Chinese serif.
    static let wordmark = Font.system(.largeTitle, design: .serif, weight: .semibold)
}

extension View {
    /// Paints a page with the canvas colour, replacing a list or form's own background.
    func canvas(grouped: Bool = false) -> some View {
        scrollContentBackground(.hidden)
            .background(grouped ? Color.groupedCanvas : Color.canvas)
    }

    /// Long-form text meant to be read: body size with open line spacing.
    func readingText() -> some View {
        font(.body).lineSpacing(6)
    }

    /// A small, quiet label above a group of content.
    func sectionLabel() -> some View {
        font(.footnote.weight(.medium))
            .foregroundStyle(.secondary)
            .textCase(nil)
    }

    /// The single primary action on a screen.
    func primaryAction() -> some View {
        buttonStyle(.glassProminent)
            .buttonBorderShape(.capsule)
            .controlSize(.large)
            .foregroundStyle(.onAccent)
    }
}

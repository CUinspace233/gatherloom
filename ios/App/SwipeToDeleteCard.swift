import SwiftUI

/// A rounded card that reveals a delete button when swiped left.
/// The first tap turns the button into 「确认删除」; the second tap deletes.
struct SwipeToDeleteCard<Content: View>: View {
    /// Whether this card is the one currently swiped open. Owned by the list so
    /// that opening one card closes the others.
    let isOpen: Bool
    let onOpenChange: (Bool) -> Void
    /// Called for a tap on a closed card. A tap on an open card closes it.
    let onTap: () -> Void
    let onDelete: () -> Void
    @ViewBuilder let content: Content

    @State private var dragOffset: CGFloat = 0
    @State private var isConfirming = false

    private let buttonSize: CGFloat = 52
    private let confirmWidth: CGFloat = 112
    private let gap: CGFloat = 12

    private var revealWidth: CGFloat { (isConfirming ? confirmWidth : buttonSize) + gap }
    private var offset: CGFloat { min(0, (isOpen ? -revealWidth : 0) + dragOffset) }

    var body: some View {
        ZStack(alignment: .trailing) {
            deleteButton
                .opacity(offset < -8 ? 1 : 0)
            content
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 24))
                .contentShape(.rect)
                .onTapGesture { isOpen ? onOpenChange(false) : onTap() }
                .accessibilityAddTraits(.isButton)
                .offset(x: offset)
        }
        .gesture(swipe)
        .animation(.snappy(duration: 0.25), value: isOpen)
        .animation(.snappy(duration: 0.25), value: isConfirming)
        .onChange(of: isOpen) { _, open in
            if !open { isConfirming = false }
        }
        .accessibilityAction(named: isConfirming ? "确认删除" : "删除") { tapDelete() }
    }

    private var deleteButton: some View {
        Button(action: tapDelete) {
            Group {
                if isConfirming {
                    Text("确认删除").font(.subheadline.weight(.semibold)).fixedSize()
                } else {
                    Image(systemName: "trash").font(.title3.weight(.semibold))
                }
            }
            .foregroundStyle(.white)
            .frame(width: isConfirming ? confirmWidth : buttonSize, height: buttonSize)
            .background(.red, in: .capsule)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(isConfirming ? "确认删除" : "删除")
    }

    private var swipe: some Gesture {
        DragGesture(minimumDistance: 20)
            .onChanged { value in
                // Leave vertical drags to the scroll view.
                guard abs(value.translation.width) > abs(value.translation.height) else { return }
                dragOffset = value.translation.width
            }
            .onEnded { value in
                let horizontal = abs(value.translation.width) > abs(value.translation.height)
                let finalOffset = (isOpen ? -revealWidth : 0) + (horizontal ? value.translation.width : 0)
                withAnimation(.snappy(duration: 0.25)) { dragOffset = 0 }
                guard horizontal else { return }
                onOpenChange(finalOffset < -buttonSize / 2)
            }
    }

    private func tapDelete() {
        if isConfirming {
            onDelete()
        } else {
            if !isOpen { onOpenChange(true) }
            isConfirming = true
        }
    }
}

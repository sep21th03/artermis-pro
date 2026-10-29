// Fixture: overlay — định nghĩa trong repo (AppModal, AppBottomSheet) và dùng Modal của
// react-native. Analyzer chỉ ghi bằng chứng, KHÔNG suy diễn trigger/cách đóng.
import React from "react";
import { View, Text, Modal } from "react-native";
import { BottomSheet } from "@gorhom/bottom-sheet";

export default function AppModal({ visible, onClose, children }) {
  return (
    <Modal visible={visible} transparent onRequestClose={onClose}>
      {children}
    </Modal>
  );
}

export function AppBottomSheet({ visible, children }) {
  return <BottomSheet visible={visible}>{children}</BottomSheet>;
}

export function SimpleDialog() {
  return <Text>dialog</Text>;
}

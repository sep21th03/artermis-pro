// Fixture: các dạng call site — literal, qua route key, có guard, có điều kiện `&&`,
// thoát sớm, đích động (không phân tích được), và đi lùi (không có đích).
import React from "react";
import { View, Text, Button } from "react-native";
import NavigationService from "../Common/NavigationService";
import { KEY_NAME_COLLECTIONS, KEY_NAME_PLACEHOLDER } from "../NavigatorManage";
import AppModal from "../components/AppModal";
import { BottomSheetModal } from "@gorhom/bottom-sheet";

export default function HomeScreen({ navigation, isLoggedIn, ready, targetRoute }) {
  const [showModal, setShowModal] = React.useState(false);
  const [sheetVisible, setSheetVisible] = React.useState(false);

  const openCollections = () => {
    navigation.navigate("Collections");
    NavigationService.navigate(KEY_NAME_COLLECTIONS);
    NavigationService.navigate(KEY_NAME_PLACEHOLDER);
    navigation.navigate(targetRoute);
    navigation.goBack();
  };

  const goProfile = () => {
    if (isLoggedIn) {
      navigation.navigate("Profile");
    }
  };

  const maybeReplace = () => ready && navigation.replace("Login");

  return (
    <View>
      <Text>Home</Text>
      <Button title="Collections" onPress={openCollections} />
      <Button title="Profile" onPress={goProfile} />
      <Button title="Replace" onPress={maybeReplace} />
      <AppModal visible={showModal} onClose={() => setShowModal(false)} />
      <BottomSheetModal visible={sheetVisible} />
    </View>
  );
}

// Fixture: guard kiểu "thoát sớm" trước call site.
import React from "react";
import { View, Text } from "react-native";

export default function LoginScreen({ navigation, user }) {
  const submit = () => {
    if (!user) return;
    navigation.navigate("Home");
  };

  return (
    <View>
      <Text>Login</Text>
    </View>
  );
}

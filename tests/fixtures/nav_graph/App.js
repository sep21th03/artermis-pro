// Fixture: route table tĩnh + deep link (linking) + navigator.
// Không phải app thật — chỉ để test analyzer.
import React from "react";
import { NavigationContainer } from "@react-navigation/native";
import { createNativeStackNavigator } from "@react-navigation/stack";
import { createBottomTabNavigator } from "@react-navigation/bottom-tabs";
import HomeScreen from "./src/screens/HomeScreen";
import LoginScreen from "./src/screens/LoginScreen";
import CollectionsScreen from "./src/screens/CollectionsScreen";
import SettingsScreen from "./src/screens/SettingsScreen";

const Stack = createNativeStackNavigator();
const Tabs = createBottomTabNavigator();

const linking = {
  prefixes: ["fixture://"],
  screens: {
    Settings: "settings",
    Collections: "collections/:id",
  },
};

export default function App() {
  return (
    <NavigationContainer linking={linking}>
      <Stack.Navigator>
        <Stack.Screen name="Login" component={LoginScreen} />
        <Stack.Screen name="Home" component={HomeScreen} />
        <Stack.Screen name="Collections" component={CollectionsScreen} />
        <Stack.Screen name="Settings" component={SettingsScreen} />
      </Stack.Navigator>
      <Tabs.Navigator>
        <Tabs.Screen name="HomeTab" component={HomeScreen} />
      </Tabs.Navigator>
    </NavigationContainer>
  );
}

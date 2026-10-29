// Fixture: service điều hướng (giống app thật: mọi call đi qua một service).
import { navigationRef } from "./navigationRef";

function navigate(name, params) {
  if (navigationRef.current) {
    navigationRef.current.navigate(name, params);
  }
}

function replace(name, params) {
  if (navigationRef.current) {
    navigationRef.current.replace(name, params);
  }
}

function goBack() {
  if (navigationRef.current) {
    navigationRef.current.goBack();
  }
}

export default { navigate, replace, goBack };

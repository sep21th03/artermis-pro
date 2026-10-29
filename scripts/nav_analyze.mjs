#!/usr/bin/env node
/**
 * nav_analyze.mjs — trích Navigation Graph của một app React Native **từ source**, bằng AST.
 *
 * Vì sao AST chứ không regex/LLM: quan hệ điều hướng phải có **bằng chứng file:line**; regex
 * trên JSX nhiều dòng sai rất nhanh, còn LLM đọc từng file thì vừa đắt vừa bịa được quan hệ.
 * Mọi node/cạnh đều kèm `evidence` lấy từ đúng vị trí AST, và chỗ không phân tích được thì ghi
 * vào `unresolved` thay vì đoán — không tạo cạnh chỉ vì hai màn hình cùng nằm trong route table.
 *
 * Không phụ thuộc ARTEMIS: script chạy bằng node của **chính project nguồn** và mượn
 * `@babel/parser` trong `node_modules` của project đó (React Native luôn có sẵn), nên không phải
 * cài thêm gói nào và không cần Metro/debug build — source tĩnh là đủ.
 *
 * Phase 1: không Hot Updater/OTA/bundle version. Graph là dữ liệu tương đối ổn định, phân tích
 * một lần rồi nạp vào KB; chỉ chạy lại khi người dùng chủ động yêu cầu.
 *
 * Dùng:
 *   node scripts/nav_analyze.mjs --source /path/to/rn-app --app-id hunonic --out nav-graph.json
 *   node scripts/nav_analyze.mjs --config config/nav_sources.json --app-id hunonic --out ...
 *   node scripts/nav_analyze.mjs --source ... --babel-from <dir có node_modules> --verbose
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";

const SCHEMA = "artemis.nav_graph/1";
const DEFAULT_EXCLUDES = [
  "**/node_modules/**",
  "**/.git/**",
  "**/ios/Pods/**",
  "**/ios/build/**",
  "**/android/build/**",
  "**/android/app/build/**",
  "**/__tests__/**",
  "**/*.test.js",
  "**/*.test.jsx",
  "**/*.test.ts",
  "**/*.test.tsx",
  "**/*.spec.js",
  "**/*.spec.ts",
];
const SOURCE_EXTS = new Set([".js", ".jsx", ".ts", ".tsx"]);

function parseArgs(argv) {
  const args = {
    source: "",
    appId: "",
    out: "",
    config: "",
    babelFrom: "",
    include: [],
    exclude: [],
    maxFiles: 6000,
    dryRun: false,
    verbose: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    const next = () => argv[++i];
    switch (token) {
      case "--source": args.source = next(); break;
      case "--app-id": args.appId = next(); break;
      case "--out": args.out = next(); break;
      case "--config": args.config = next(); break;
      case "--babel-from": args.babelFrom = next(); break;
      case "--include": args.include.push(next()); break;
      case "--exclude": args.exclude.push(next()); break;
      case "--max-files": args.maxFiles = Number(next()); break;
      case "--dry-run": args.dryRun = true; break;
      case "--verbose": args.verbose = true; break;
      case "-h":
      case "--help": args.help = true; break;
      default:
        if (token.startsWith("--")) throw new Error(`tham số lạ: ${token}`);
    }
  }
  return args;
}

/** Cấu hình theo app: { apps: { <app_id>: {source, include, exclude} } } — không hardcode app nào. */
function loadConfig(configPath) {
  if (!configPath) return { apps: {} };
  const data = JSON.parse(fs.readFileSync(configPath, "utf8"));
  return data && typeof data === "object" ? data : { apps: {} };
}

function globToRegExp(glob) {
  const escaped = glob
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*\*\//g, "::SLASH::")
    .replace(/\*\*/g, "::ANY::")
    .replace(/\*/g, "[^/]*")
    .replace(/\?/g, "[^/]")
    .replace(/::SLASH::/g, "(?:.*/)?")
    .replace(/::ANY::/g, ".*");
  return new RegExp(`^${escaped}$`);
}

function matchesAny(relPath, patterns) {
  return patterns.some((pattern) => globToRegExp(pattern).test(relPath));
}

function walkSource(root, { include, exclude, maxFiles }) {
  const files = [];
  const skipped = [];
  const stack = [root];
  while (stack.length) {
    const current = stack.pop();
    let entries = [];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (error) {
      skipped.push({ path: current, reason: `readdir: ${error.message}` });
      continue;
    }
    for (const entry of entries) {
      const absolute = path.join(current, entry.name);
      const rel = path.relative(root, absolute);
      if (entry.isDirectory()) {
        if (matchesAny(`${rel}/`, DEFAULT_EXCLUDES) || matchesAny(`${rel}/`, exclude)) continue;
        stack.push(absolute);
        continue;
      }
      if (!entry.isFile()) continue;
      if (!SOURCE_EXTS.has(path.extname(entry.name))) continue;
      if (matchesAny(rel, DEFAULT_EXCLUDES) || matchesAny(rel, exclude)) continue;
      if (include.length && !matchesAny(rel, include)) continue;
      files.push({ absolute, rel });
      if (files.length >= maxFiles) {
        files.sort((a, b) => a.rel.localeCompare(b.rel));
        return { files, skipped, truncated: true };
      }
    }
  }
  files.sort((a, b) => a.rel.localeCompare(b.rel));
  return { files, skipped, truncated: false };
}

/** Mượn @babel của chính project nguồn (không thêm dependency cho ARTEMIS).
 *
 * `babelFrom` cho phép chỉ định project có `node_modules/@babel` khi thư mục phân tích không có
 * (fixture test, hoặc source tách rời khỏi node_modules).
 */
function loadBabel(sourceRoot, babelFrom = "") {
  const roots = [babelFrom, sourceRoot].filter(Boolean);
  const candidates = [];
  for (const root of roots) {
    try {
      candidates.push(createRequire(path.join(root, "package.json")));
    } catch {
      /* không có package.json — thử tiếp */
    }
  }
  candidates.push(createRequire(import.meta.url));
  let lastError = null;
  for (const req of candidates) {
    try {
      return {
        parser: req("@babel/parser"),
        traverse: req("@babel/traverse").default || req("@babel/traverse"),
      };
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(
    `Không nạp được @babel/parser trong ${roots.join(", ")} (${lastError && lastError.message}). `
      + "Chạy `yarn install` trong project nguồn, hoặc dùng --babel-from <dir có node_modules>.",
  );
}

// ---------------------------------------------------------------------------
// Tiện ích AST
// ---------------------------------------------------------------------------

function snippetOf(code, node, limit = 160) {
  if (!node || node.start == null || node.end == null) return "";
  const text = code.slice(node.start, node.end).replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function jsxNameOf(node) {
  if (!node || !node.name) return "";
  if (node.name.type === "JSXIdentifier") return node.name.name;
  if (node.name.type === "JSXMemberExpression") {
    const object = node.name.object.type === "JSXIdentifier" ? node.name.object.name : "";
    return object ? `${object}.${node.name.property.name}` : node.name.property.name;
  }
  return "";
}

function attrOf(openingElement, name) {
  for (const attr of openingElement.attributes || []) {
    if (attr.type !== "JSXAttribute" || !attr.name) continue;
    if (attr.name.name === name) return attr;
  }
  return null;
}

function staticStringOf(node) {
  if (!node) return null;
  if (node.type === "StringLiteral") return node.value;
  if (node.type === "TemplateLiteral" && (node.expressions || []).length === 0) {
    return (node.quasis || []).map((q) => q.value.cooked ?? q.value.raw).join("");
  }
  if (node.type === "JSXExpressionContainer") return staticStringOf(node.expression);
  return null;
}

/** Đường dẫn member đầy đủ: `navigationRef.current.navigate`, `this.props.navigation.push`.
 *
 * Phải giữ cả chuỗi vì chỉ `navigate` trần thì không phân biệt được điều hướng với
 * `array.push(...)`; bản cũ trả về mỗi tên property nên `navigationRef.current.navigate(...)`
 * bị loại khỏi kết quả (đo trên fixture: 3 call site của NavigationService.js mất tích).
 */
function memberPathOf(node) {
  if (!node) return "";
  if (node.type === "Identifier") return node.name;
  if (node.type === "ThisExpression") return "this";
  if (node.type === "MemberExpression") {
    const left = memberPathOf(node.object);
    const right = node.property && node.property.name ? node.property.name : "";
    if (left && right) return `${left}.${right}`;
    return right || left;
  }
  if (node.type === "CallExpression") return memberPathOf(node.callee);
  return "";
}

function callNameOf(node) {
  const callee = node && node.callee;
  if (!callee) return "";
  if (callee.type === "Identifier") return callee.name;
  if (callee.type === "MemberExpression") {
    const object = memberPathOf(callee.object);
    const property = callee.property && callee.property.name ? callee.property.name : "";
    return object ? `${object}.${property}` : property;
  }
  return "";
}

const NAVIGATOR_FACTORIES = [
  "createNativeStackNavigator",
  "createStackNavigator",
  "createBottomTabNavigator",
  "createMaterialTopTabNavigator",
  "createDrawerNavigator",
];

// ---------------------------------------------------------------------------
// Pass 1 — route registry: màn hình, route key, navigator, deep link
// ---------------------------------------------------------------------------

// Pass 1a — bảng route key: `export const KEY_NAME_X = 'route'`
// ---------------------------------------------------------------------------

/** Bảng key phải có **trước** pass đăng ký màn hình: app thật đăng ký route bằng biến
 * (`<Stack.Screen name={KEY_NAME_SPLASH} component={SplashScreenNew} />`). Đo trên app-hunonic:
 * bỏ bước này thì chỉ đọc được 2/407 màn hình.
 */
function collectKeys(babel, ast, code, file, registry) {
  const { traverse } = babel;
  traverse(ast, {
    VariableDeclarator(nodePath) {
      const { node } = nodePath;
      if (!node.id || node.id.type !== "Identifier" || !node.init) return;
      const key = node.id.name;
      if (!/KEY|ROUTE|SCREEN/i.test(key)) return;
      const value = staticStringOf(node.init);
      if (value == null) return;
      registry.keys[key] = {
        route: value,
        // Cờ *cảnh báo*, không phải lý do để bỏ qua: nhiều app đặt route string trùng tên hằng
        // (`KEY_NAME_X = 'KEY_NAME_X'`) và giá trị đó vẫn là route thật đang chạy.
        placeholder: value === key,
        file,
        line: node.loc ? node.loc.start.line : null,
      };
    },
  });
}

// ---------------------------------------------------------------------------
// Pass 1b — route registry: màn hình, navigator, deep link
// ---------------------------------------------------------------------------

function collectRegistry(babel, ast, code, file, registry) {
  const { traverse } = babel;

  // (a) Đăng ký màn hình qua JSX: name="X" hoặc name={KEY_NAME_X}
  traverse(ast, {
    JSXElement(nodePath) {
      const { node } = nodePath;
      const name = jsxNameOf(node.openingElement);
      if (name !== "Screen" && !name.endsWith(".Screen")) return;
      const nameAttr = attrOf(node.openingElement, "name");
      const valueNode = nameAttr && nameAttr.value;
      let route = staticStringOf(valueNode);
      let routeConstant = null;
      if (route == null && valueNode && valueNode.type === "JSXExpressionContainer"
        && valueNode.expression.type === "Identifier"
        && registry.keys[valueNode.expression.name]) {
        routeConstant = valueNode.expression.name;
        route = registry.keys[routeConstant].route;
      }
      if (!route) return;
      const componentAttr = attrOf(node.openingElement, "component");
      const component = componentAttr && componentAttr.value
        && componentAttr.value.type === "JSXExpressionContainer"
        ? (componentAttr.value.expression.name || "")
        : "";
      registry.screens.push({
        route,
        route_constant: routeConstant,
        component,
        navigator: name.endsWith(".Screen") ? name.split(".")[0] : "unknown",
        source: "route_table",
        file,
        line: node.loc ? node.loc.start.line : null,
        evidence: snippetOf(code, node),
      });
    },
  });

  // (b) Bảng route key tập trung: export const KEY_NAME_X = 'route'
  // (b) Bảng route key do `collectKeys` (pass 1a) thu — xem hàm đó.
  void registry.keys;

  // (c) Navigator: createXNavigator()
  traverse(ast, {
    CallExpression(nodePath) {
      const { node } = nodePath;
      const callee = node.callee;
      if (!callee || callee.type !== "Identifier") return;
      if (!NAVIGATOR_FACTORIES.includes(callee.name)) return;
      const parent = nodePath.parentPath && nodePath.parentPath.node;
      const variable = parent && parent.type === "VariableDeclarator" && parent.id
        && parent.id.type === "Identifier" ? parent.id.name : "";
      registry.navigators.push({
        factory: callee.name,
        variable,
        file,
        line: node.loc ? node.loc.start.line : null,
        evidence: snippetOf(code, node),
      });
    },
  });

  // (d) Deep link: linking={{ screens: { Route: 'path' } }}
  traverse(ast, {
    ObjectProperty(nodePath) {
      const { node } = nodePath;
      const key = node.key && (node.key.name || node.key.value);
      if (key !== "screens" || !node.value || node.value.type !== "ObjectExpression") return;
      for (const property of node.value.properties || []) {
        if (property.type !== "ObjectProperty") continue;
        const route = property.key && (property.key.name || property.key.value);
        if (!route || typeof route !== "string") continue;
        const deepLink = staticStringOf(property.value);
        registry.screens.push({
          route,
          component: "",
          navigator: "linking",
          source: "linking",
          deep_link: deepLink,
          file,
          line: property.loc ? property.loc.start.line : null,
          evidence: snippetOf(code, property),
        });
      }
    },
  });
}

// ---------------------------------------------------------------------------
// Pass 1c — đồ thị render: component nào được render bởi component nào
// ---------------------------------------------------------------------------

/** Vì sao cần: call site thật thường nằm trong component con
 * (`src/Components/Home/HomeHeader.js` gọi `NavigationService.navigate(...)`), không nằm trong
 * component màn hình. Không có quan hệ render thì không biết cạnh xuất phát từ màn hình nào —
 * đo trên app-hunonic: 573 call site rơi vào trường hợp này.
 */
function collectRenderEdges(babel, ast, code, file, out) {
  const { traverse } = babel;
  traverse(ast, {
    JSXElement(nodePath) {
      const { node } = nodePath;
      const raw = jsxNameOf(node.openingElement);
      if (!raw) return;
      const used = raw.includes(".") ? raw.split(".").pop() : raw;
      // Chỉ component (viết hoa) — bỏ thẻ HTML/RN primitive như View, Text.
      if (!/^[A-Z]/.test(used)) return;
      const inside = enclosingComponent(nodePath, null).name;
      if (!inside) return;
      const list = out.usage.get(used) || [];
      list.push({ inside, file, line: node.loc ? node.loc.start.line : null });
      out.usage.set(used, list);
    },
  });
}

/** Đi ngược đồ thị render tìm màn hình đã đăng ký gần nhất (có trần độ sâu). */
function resolveSourceScreen(componentName, screensByComponent, usageIndex, maxDepth = 4) {
  if (!componentName) return null;
  if (screensByComponent[componentName]) {
    return {
      screen: screensByComponent[componentName],
      source: "enclosing_component",
      path: [componentName],
    };
  }
  const seen = new Set([componentName]);
  const path = [componentName];
  let frontier = [componentName];
  for (let depth = 0; depth < maxDepth; depth += 1) {
    const next = [];
    for (const name of frontier) {
      for (const usage of usageIndex.get(name) || []) {
        if (!usage.inside || seen.has(usage.inside)) continue;
        seen.add(usage.inside);
        path.push(usage.inside);
        if (screensByComponent[usage.inside]) {
          return {
            screen: screensByComponent[usage.inside],
            source: "render_graph",
            path: [...path],
            evidence: usage,
          };
        }
        next.push(usage.inside);
      }
    }
    if (!next.length) break;
    frontier = next;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Pass 2 — navigation call sites, guard conditions, bằng chứng file:line
// ---------------------------------------------------------------------------

/** Loại call → ý nghĩa. `history` là đi lùi/đóng: **không** có đích xác định nên không tạo cạnh. */
const NAV_CALL_KINDS = {
  navigate: "open",
  push: "open",
  replace: "replace",
  reset: "reset",
  pop: "history",
  popToTop: "history",
  goBack: "history",
  dispatch: "dispatch",
};

/** Tên hàm/component gần nhất bao quanh node, và nó có phải màn hình đã đăng ký hay không.
 *
 * Call site thường nằm trong hàm con (`const openX = () => { navigate(...) }`) nên chỉ lấy hàm gần
 * nhất là sai: phải đi tiếp lên tới **component đã có trong route table** mới biết màn hình nguồn.
 */
function enclosingComponent(nodePath, knownComponents = null) {
  let current = nodePath;
  let fallback = "";
  while (current) {
    const { node } = current;
    const isFunction = node.type === "FunctionDeclaration"
      || node.type === "FunctionExpression"
      || node.type === "ArrowFunctionExpression"
      || node.type === "ClassMethod"
      || node.type === "ObjectMethod";
    if (isFunction) {
      let name = "";
      if (node.id && node.id.name) name = node.id.name;
      if (!name) {
        const parent = current.parentPath && current.parentPath.node;
        if (parent && parent.type === "VariableDeclarator" && parent.id
          && parent.id.type === "Identifier") name = parent.id.name;
        else if (parent && parent.type === "ObjectProperty" && parent.key) {
          name = parent.key.name || parent.key.value || "";
        } else if (node.key && node.key.name) name = node.key.name;
      }
      if (name) {
        if (knownComponents && knownComponents.has(name)) return { name, isScreen: true };
        if (!fallback) fallback = name;
      }
    }
    current = current.parentPath;
  }
  return { name: fallback, isScreen: false };
}

/** True khi `child` nằm trong cây con `container` (so theo khoảng offset của AST). */
function isInside(child, container) {
  return Boolean(
    child && container && child.start != null && container.start != null
    && child.start >= container.start && child.end <= container.end,
  );
}

function guardEntries(nodePath, code) {
  const guards = [];
  let current = nodePath;
  while (current) {
    const parent = current.parentPath;
    if (!parent) break;
    const node = parent.node;
    if (node.type === "IfStatement" && node.test) {
      const inBranch = isInside(nodePath.node, node.consequent)
        || isInside(nodePath.node, node.alternate || {});
      if (inBranch) {
        guards.push({
          text: snippetOf(code, node.test, 120),
          polarity: isInside(nodePath.node, node.alternate || {}) ? "else" : "then",
          line: node.test.loc ? node.test.loc.start.line : null,
          evidence: snippetOf(code, node, 200),
        });
      }
    } else if (node.type === "ConditionalExpression" && node.test) {
      const inBranch = isInside(nodePath.node, node.consequent)
        || isInside(nodePath.node, node.alternate);
      if (inBranch) {
        guards.push({
          text: snippetOf(code, node.test, 120),
          polarity: isInside(nodePath.node, node.alternate) ? "else" : "then",
          line: node.test.loc ? node.test.loc.start.line : null,
          evidence: snippetOf(code, node, 200),
        });
      }
    } else if (node.type === "LogicalExpression"
      && (node.operator === "&&" || node.operator === "||")) {
      if (isInside(nodePath.node, node.right)) {
        guards.push({
          text: snippetOf(code, node.left, 120),
          polarity: node.operator === "&&" ? "then" : "or",
          line: node.left.loc ? node.left.loc.start.line : null,
          evidence: snippetOf(code, node, 200),
        });
      }
    }
    const isFunction = node.type === "FunctionDeclaration"
      || node.type === "FunctionExpression"
      || node.type === "ArrowFunctionExpression"
      || node.type === "ClassMethod"
      || node.type === "ObjectMethod";
    if (isFunction) break; // guard của component khác không áp cho call site này
    current = parent;
  }
  return guards;
}

/** Guard kiểu "thoát sớm": `if (!ready) return;` đứng TRƯỚC call site trong cùng khối. */
function earlyReturnGuards(nodePath, code) {
  const guards = [];
  // Đi lên tới BlockStatement đầu tiên (call thường nằm trong ExpressionStatement/AwaitExpression).
  let container = nodePath.parentPath;
  while (container && container.node.type !== "BlockStatement") {
    const node = container.node;
    const isFunction = node.type === "FunctionDeclaration"
      || node.type === "FunctionExpression"
      || node.type === "ArrowFunctionExpression"
      || node.type === "ClassMethod"
      || node.type === "ObjectMethod";
    if (isFunction) return guards; // hết phạm vi hàm mà chưa gặp khối
    container = container.parentPath;
  }
  if (!container) return guards;
  const block = container.node;
  if (!Array.isArray(block.body)) return guards;
  const callStart = nodePath.node.start;
  for (const statement of block.body) {
    if (statement.start == null || statement.start >= callStart) break;
    if (statement.type !== "IfStatement" || statement.alternate) continue;
    const consequent = statement.consequent;
    const exits = consequent
      && (consequent.type === "ReturnStatement" || consequent.type === "ThrowStatement");
    if (!exits || !statement.test) continue;
    guards.push({
      text: snippetOf(code, statement.test, 120),
      polarity: "early_return",
      line: statement.test.loc ? statement.test.loc.start.line : null,
      evidence: snippetOf(code, statement, 200),
    });
  }
  return guards;
}

/** Tên cục bộ thực sự là điều hướng trong file này.
 *
 * Vì sao cần: nếu chỉ xét tên method thì `.push({...})` của **mảng** và `dispatch(action)` của
 * **Redux** đều bị coi là điều hướng. Đo trên app-hunonic: 2.974 "đích động" thì đa số là dương
 * tính giả kiểu `this.listPeripheralFail.push({...})`, `dispatch(setTextError(...))`.
 */
function collectNavigatorBindings(babel, ast) {
  const { traverse } = babel;
  const names = new Set();
  const hookNames = new Set(["useNavigation", "useNavigationService", "useNavigator"]);
  traverse(ast, {
    ImportDeclaration(nodePath) {
      const { node } = nodePath;
      const source = String(node.source && node.source.value);
      for (const specifier of node.specifiers || []) {
        const local = specifier.local && specifier.local.name;
        if (!local) continue;
        const imported = specifier.imported ? specifier.imported.name : "default";
        if (hookNames.has(imported)) {
          hookNames.add(local);
          continue;
        }
        // `import NavigationService from '.../NavigationService'` hoặc navigationRef.
        if (/navigation/i.test(source) || /navigation/i.test(local)) names.add(local);
      }
    },
    VariableDeclarator(nodePath) {
      const { node } = nodePath;
      if (!node.init) return;
      const initName = callNameOf(node.init);
      const fromHook = initName && hookNames.has(initName);
      const fromProps = /navigation/i.test(initName) || /navigation/i.test(snippetOfSafe(node.init));
      if (fromHook && node.id.type === "Identifier") {
        names.add(node.id.name);
        return;
      }
      if (fromHook && node.id.type === "ObjectPattern") {
        for (const property of node.id.properties || []) {
          if (property.key && property.key.name) names.add(property.key.name);
        }
        return;
      }
      if (fromProps && node.id.type === "Identifier") names.add(node.id.name);
    },
    ObjectPattern(nodePath) {
      // `function Screen({ navigation })` / `({ navigation, route })`
      for (const property of nodePath.node.properties || []) {
        if (property.key && property.key.name === "navigation") names.add("navigation");
      }
    },
  });
  return names;
}

function snippetOfSafe(node) {
  try {
    return JSON.stringify(node).slice(0, 200);
  } catch {
    return "";
  }
}

/** Đối số của call điều hướng: string, hằng route, `{name: ...}` hoặc `StackActions.x(...)`. */
function resolveTarget(arg, out) {
  const direct = staticStringOf(arg);
  if (direct != null) return { route: direct, key: null, placeholder: false };
  if (arg && arg.type === "Identifier" && out.keys[arg.name]) {
    return {
      route: out.keys[arg.name].route,
      key: arg.name,
      placeholder: Boolean(out.keys[arg.name].placeholder),
    };
  }
  if (arg && arg.type === "MemberExpression" && arg.property && arg.property.name
    && out.keys[arg.property.name]) {
    // `NavigatorManage.KEY_NAME_X`
    return {
      route: out.keys[arg.property.name].route,
      key: arg.property.name,
      placeholder: Boolean(out.keys[arg.property.name].placeholder),
    };
  }
  if (arg && arg.type === "ObjectExpression") {
    // `navigation.navigate({ name: KEY_NAME_X, params: {...} })`
    for (const property of arg.properties || []) {
      if (property.type !== "ObjectProperty") continue;
      const key = property.key && (property.key.name || property.key.value);
      if (key !== "name") continue;
      return resolveTarget(property.value, out);
    }
  }
  if (arg && arg.type === "CallExpression" && arg.arguments && arg.arguments[0]) {
    // `navigation.dispatch(StackActions.replace('X', {...}))`
    return resolveTarget(arg.arguments[0], out);
  }
  return null;
}

function collectCallSites(babel, ast, code, file, screensByComponent, out) {
  const { traverse } = babel;
  const navigatorNames = collectNavigatorBindings(babel, ast);
  traverse(ast, {
    CallExpression(nodePath) {
      const { node } = nodePath;
      const full = callNameOf(node);
      if (!full) return;
      const method = full.includes(".") ? full.split(".").pop() : full;
      if (!(method in NAV_CALL_KINDS)) return;
      const object = full.includes(".") ? full.split(".").slice(0, -1).join(".") : "";
      // Điều hướng khi: gọi trên đối tượng tên na ná navigation, **hoặc** tên trần đã được gán từ
      // hook điều hướng. Nếu không, `.push()` của mảng / `dispatch()` của Redux bị nhận nhầm.
      const looksNavigational = /navigation|navigator|nav/i.test(object)
        || navigatorNames.has(full);
      if (!looksNavigational) return;

      const arg = (node.arguments || [])[0] || null;
      const resolvedTarget = resolveTarget(arg, out);
      const targetRoute = resolvedTarget ? resolvedTarget.route : null;
      const targetKey = resolvedTarget ? resolvedTarget.key : null;
      const keyPlaceholder = resolvedTarget ? resolvedTarget.placeholder : false;
      const resolved = Boolean(resolvedTarget);

      const componentInfo = enclosingComponent(nodePath, out.componentNames || null);
      const component = componentInfo.name;
      const fromInfo = resolveSourceScreen(
        component,
        out.screensByComponent || {},
        out.usageIndex || new Map(),
      );
      const fromScreen = fromInfo ? fromInfo.screen : null;
      const guards = [...guardEntries(nodePath, code), ...earlyReturnGuards(nodePath, code)]
        .map((guard) => ({ ...guard, file }));
      const evidence = {
        file,
        line: node.loc ? node.loc.start.line : null,
        snippet: snippetOf(code, node),
      };
      const kind = NAV_CALL_KINDS[method];

      if (kind === "history") {
        out.unresolved.push({
          kind: "history_navigation",
          file,
          line: evidence.line,
          detail: `${full}() — đi lùi/đóng, không có đích xác định nên không tạo cạnh`,
          snippet: evidence.snippet,
        });
        return;
      }
      if (!resolved) {
        out.unresolved.push({
          kind: "dynamic_target",
          file,
          line: evidence.line,
          detail: `${full}() với đối số không tĩnh (${arg ? arg.type : "không có"})`,
          snippet: evidence.snippet,
        });
        return;
      }
      if (!fromScreen) {
        out.unresolved.push({
          kind: "no_source_screen",
          file,
          line: evidence.line,
          detail: `không xác định được màn hình nguồn (component "${component || "ẩn danh"}" không nằm trong route table và không đi ngược được đồ thị render)`,
          snippet: evidence.snippet,
        });
        return;
      }
      // Đích phải là màn hình **đã đăng ký**: một số app đặt hằng route trùng tên hằng, giá trị đó
      // vẫn hợp lệ; nhưng nếu route không hề có trong route table thì đây là dữ liệu cần người xem.
      const toRegistered = (out.registeredRoutes || new Set()).has(
        String(targetRoute).toLowerCase(),
      );
      if (!toRegistered) {
        out.unresolved.push({
          kind: "target_not_registered",
          file,
          line: evidence.line,
          detail: `đích "${targetRoute}" không có trong route table (route key ${targetKey || "literal"})`,
          snippet: evidence.snippet,
        });
      }
      out.edges.push({
        from: fromScreen,
        to: targetRoute,
        key: targetKey,
        key_placeholder: keyPlaceholder,
        to_registered: toRegistered,
        kind,
        call: full,
        component: component || null,
        from_source: fromInfo.source,
        from_path: fromInfo.path,
        from_evidence: fromInfo.evidence || null,
        guards,
        evidence: [evidence],
        confidence: Math.round(
          ((guards.length ? 0.6 : 0.9)
            - (fromInfo.source === "render_graph" ? 0.2 : 0)
            - (toRegistered ? 0 : 0.4)) * 100,
        ) / 100,
      });
    },
  });
}

// ---------------------------------------------------------------------------
// Pass 3 — modal / bottomsheet / dialog / popup (KHÔNG suy diễn trigger/đóng)
// ---------------------------------------------------------------------------

const OVERLAY_IMPORTS = {
  "react-native": { Modal: "modal" },
  "@gorhom/bottom-sheet": { BottomSheet: "bottomsheet", BottomSheetModal: "bottomsheet" },
  "react-native-modal": { default: "modal" },
};

function kindFromName(name) {
  const lower = (name || "").toLowerCase();
  if (/bottomsheet|bottom_sheet|bottom-sheet|actionsheet/.test(lower)) return "bottomsheet";
  if (/dialog/.test(lower)) return "dialog";
  if (/popup|popover/.test(lower)) return "popup";
  if (/modal/.test(lower)) return "modal";
  return "";
}

function collectOverlays(babel, ast, code, file, screensByComponent, out) {
  const { traverse } = babel;
  const localKinds = new Map();

  // (a) Import: tên cục bộ nào là overlay và loại của nó.
  traverse(ast, {
    ImportDeclaration(nodePath) {
      const { node } = nodePath;
      const source = node.source && node.source.value;
      const known = OVERLAY_IMPORTS[source] || null;
      for (const specifier of node.specifiers || []) {
        const local = specifier.local && specifier.local.name;
        if (!local) continue;
        const imported = specifier.imported ? specifier.imported.name : "default";
        let kind = known ? known[imported] : "";
        if (!kind) kind = kindFromName(local) || kindFromName(imported);
        if (kind) localKinds.set(local, kind);
      }
    },
  });

  // (b) Định nghĩa component overlay trong repo (bằng chứng, không suy diễn hành vi).
  traverse(ast, {
    FunctionDeclaration(nodePath) {
      const name = nodePath.node.id && nodePath.node.id.name;
      const kind = kindFromName(name);
      if (!kind) return;
      out.overlays.push({
        name,
        kind,
        defined: true,
        file,
        line: nodePath.node.loc ? nodePath.node.loc.start.line : null,
        evidence: snippetOf(code, nodePath.node, 160),
      });
      localKinds.set(name, kind);
    },
    VariableDeclarator(nodePath) {
      const { node } = nodePath;
      if (!node.id || node.id.type !== "Identifier" || !node.init) return;
      const kind = kindFromName(node.id.name);
      if (!kind) return;
      const isComponent = ["ArrowFunctionExpression", "FunctionExpression"].includes(node.init.type)
        || (node.init.type === "CallExpression" && callNameOf(node.init).includes("forwardRef"))
        || (node.init.type === "CallExpression" && callNameOf(node.init).includes("memo"));
      if (!isComponent) return;
      out.overlays.push({
        name: node.id.name,
        kind,
        defined: true,
        file,
        line: node.loc ? node.loc.start.line : null,
        evidence: snippetOf(code, node, 160),
      });
      localKinds.set(node.id.name, kind);
    },
  });

  // (c) Chỗ dùng: chỉ ghi bằng chứng thấy được; KHÔNG suy ra trigger hay cách đóng.
  traverse(ast, {
    JSXElement(nodePath) {
      const { node } = nodePath;
      const name = jsxNameOf(node.openingElement);
      const short = name.includes(".") ? name.split(".").pop() : name;
      const kind = localKinds.get(short) || kindFromName(short);
      if (!kind) return;
      const visibleAttr = attrOf(node.openingElement, "visible")
        || attrOf(node.openingElement, "isVisible")
        || attrOf(node.openingElement, "modalVisible");
      const componentInfo = enclosingComponent(
        nodePath,
        new Set(Object.keys(screensByComponent || {})),
      );
      // Cùng cách với call site: overlay nằm trong component con thì đi ngược đồ thị render để biết
      // nó thuộc màn hình nào (`modal.<screen>.<name>` thay vì `modal.global.<name>`).
      const fromInfo = resolveSourceScreen(
        componentInfo.name,
        screensByComponent || {},
        out.usageIndex || new Map(),
      );
      out.overlays.push({
        name,
        kind,
        defined: false,
        used_in_component: componentInfo.name || null,
        used_in_screen: fromInfo ? fromInfo.screen : null,
        used_in_source: fromInfo ? fromInfo.source : null,
        visible_prop: visibleAttr
          ? snippetOf(code, visibleAttr.value || visibleAttr, 80)
          : null,
        trigger: null, // Phase 1: không suy diễn — chờ xác minh trên UI thật.
        file,
        line: node.loc ? node.loc.start.line : null,
        evidence: snippetOf(code, node, 160),
      });
    },
  });
}

// ---------------------------------------------------------------------------
// Lắp ráp kết quả
// ---------------------------------------------------------------------------

function slugify(text) {
  const folded = (text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  const slug = folded.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return slug.slice(0, 60) || "unknown";
}

function assemble(registry, calls, overlays, meta) {
  const screensByRoute = new Map();
  for (const screen of registry.screens) {
    const existing = screensByRoute.get(screen.route) || {
      route: screen.route,
      key: slugify(screen.route),
      components: [],
      navigators: [],
      sources: [],
      deep_link: null,
      evidence: [],
    };
    if (screen.component && !existing.components.includes(screen.component)) {
      existing.components.push(screen.component);
    }
    if (screen.navigator && !existing.navigators.includes(screen.navigator)) {
      existing.navigators.push(screen.navigator);
    }
    if (!existing.sources.includes(screen.source)) existing.sources.push(screen.source);
    if (screen.deep_link && !existing.deep_link) existing.deep_link = screen.deep_link;
    existing.evidence.push({ file: screen.file, line: screen.line, snippet: screen.evidence });
    screensByRoute.set(screen.route, existing);
  }

  // Gộp cạnh theo (from → to, kind): nhiều call site = nhiều bằng chứng cho **một** cạnh.
  const edgesByPair = new Map();
  for (const edge of calls.edges) {
    const key = `${edge.from}->${edge.to}|${edge.kind}`;
    const existing = edgesByPair.get(key) || {
      from: edge.from,
      to: edge.to,
      kind: edge.kind,
      calls: [],
      guards: [],
      evidence: [],
      confidence: edge.confidence,
      to_registered: edge.to_registered,
      key_placeholder: Boolean(edge.key_placeholder),
      from_source: edge.from_source,
      from_path: edge.from_path,
    };
    if (!existing.calls.includes(edge.call)) existing.calls.push(edge.call);
    existing.confidence = Math.min(existing.confidence, edge.confidence);
    existing.key_placeholder = existing.key_placeholder || Boolean(edge.key_placeholder);
    // Bằng chứng mạnh hơn thắng: call site nằm ngay trong component màn hình > phải đi qua đồ thị render.
    if (edge.from_source === "enclosing_component") {
      existing.from_source = edge.from_source;
      existing.from_path = edge.from_path;
    }
    for (const guard of edge.guards) {
      const duplicate = existing.guards.some(
        (item) => item.file === guard.file && item.line === guard.line && item.text === guard.text,
      );
      if (!duplicate) existing.guards.push(guard);
    }
    existing.evidence.push(...edge.evidence);
    edgesByPair.set(key, existing);
  }

  const overlaysByKey = new Map();
  for (const overlay of overlays) {
    overlaysByKey.set(`${overlay.kind}|${overlay.name}|${overlay.file}|${overlay.line}`, overlay);
  }

  const screens = [...screensByRoute.values()].sort((a, b) => a.key.localeCompare(b.key));

  // Chuẩn hoá tên đích/nguồn theo route table: route key có thể khác hoa/thường với route đã
  // đăng ký (`KEY_NAME_COLLECTIONS = 'collections'` vs `<Stack.Screen name="Collections">`), và
  // nếu để nguyên thì cùng một màn hình thành hai node.
  const canonicalByLower = new Map();
  for (const screen of screens) canonicalByLower.set(screen.route.toLowerCase(), screen.route);
  const canonical = (route) => canonicalByLower.get(String(route).toLowerCase()) || route;

  for (const edge of edgesByPair.values()) {
    edge.from = canonical(edge.from);
    edge.to = canonical(edge.to);
  }
  // Gộp lần hai: sau khi chuẩn hoá, hai cạnh có thể trùng nhau.
  const mergedEdges = new Map();
  for (const edge of edgesByPair.values()) {
    const key = `${edge.from}->${edge.to}|${edge.kind}`;
    const existing = mergedEdges.get(key);
    if (!existing) {
      mergedEdges.set(key, edge);
      continue;
    }
    existing.calls = [...new Set([...existing.calls, ...edge.calls])];
    existing.evidence.push(...edge.evidence);
    for (const guard of edge.guards) {
      const duplicate = existing.guards.some(
        (item) => item.file === guard.file && item.line === guard.line && item.text === guard.text,
      );
      if (!duplicate) existing.guards.push(guard);
    }
    existing.confidence = Math.min(existing.confidence, edge.confidence);
  }
  const edges = [...mergedEdges.values()].sort(
    (a, b) => a.from.localeCompare(b.from) || a.to.localeCompare(b.to),
  );
  const overlaysList = [...overlaysByKey.values()];

  const unresolvedByKind = {};
  for (const item of calls.unresolved) {
    unresolvedByKind[item.kind] = (unresolvedByKind[item.kind] || 0) + 1;
  }

  const counts = {
    screens: screens.length,
    screens_from_route_table: screens.filter((s) => s.sources.includes("route_table")).length,
    screens_from_linking: screens.filter((s) => s.sources.includes("linking")).length,
    route_keys: Object.keys(registry.keys).length,
    route_keys_placeholder: Object.values(registry.keys).filter((item) => item.placeholder).length,
    navigators: registry.navigators.length,
    screens_with_route_constant: registry.screens.filter((screen) => screen.route_constant).length,
    edges_with_evidence: edges.length,
    edges_to_unregistered_targets: edges.filter((edge) => edge.to_registered === false).length,
    edges_from_render_graph: edges.filter((edge) => edge.from_source === "render_graph").length,
    edges_with_guards: edges.filter((edge) => edge.guards.length > 0).length,
    guards: edges.reduce((total, edge) => total + edge.guards.length, 0),
    unresolved: calls.unresolved.length,
    unresolved_by_kind: unresolvedByKind,
    overlays_defined: overlaysList.filter((item) => item.defined).length,
    overlays_used: overlaysList.filter((item) => !item.defined).length,
    overlays_by_kind: overlaysList.reduce((acc, item) => {
      acc[item.kind] = (acc[item.kind] || 0) + 1;
      return acc;
    }, {}),
    parse_errors: meta.parse_errors.length,
  };

  return {
    schema: SCHEMA,
    generated_at: new Date().toISOString(),
    source: {
      root: meta.root,
      app_id: meta.app_id,
      files_scanned: meta.files_scanned,
      files_parsed: meta.files_parsed,
      truncated: meta.truncated,
      analyzer: {
        script: "scripts/nav_analyze.mjs",
        node: process.version,
        babel: meta.babel_version,
        mode: "ast",
      },
    },
    counts,
    screens,
    edges,
    overlays: overlaysList,
    unresolved: calls.unresolved,
    parse_errors: meta.parse_errors,
  };
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

const BASE_PLUGINS = [
  "jsx",
  "classProperties",
  "classPrivateProperties",
  "classPrivateMethods",
  "decorators-legacy",
  "dynamicImport",
  "exportDefaultFrom",
  "exportNamespaceFrom",
  "topLevelAwait",
  "optionalChaining",
  "nullishCoalescingOperator",
  "objectRestSpread",
  "numericSeparator",
];

/** TypeScript và Flow loại trừ nhau: thử lần lượt rồi tới không plugin (JS thuần). */
function parseWithFallback(babel, code, rel) {
  const attempts = [
    [...BASE_PLUGINS, "typescript"],
    [...BASE_PLUGINS, "flow", "flowComments"],
    [...BASE_PLUGINS],
  ];
  let lastError = null;
  for (const plugins of attempts) {
    try {
      return babel.parser.parse(code, {
        sourceType: "unambiguous",
        allowReturnOutsideFunction: true,
        errorRecovery: true,
        sourceFilename: rel,
        plugins,
      });
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError;
}

function resolveSource(args, config) {
  const fromConfig = (config.apps && args.appId && config.apps[args.appId]) || {};
  const source = args.source || fromConfig.source || "";
  if (!source) {
    throw new Error(
      "Không có đường dẫn source: dùng --source <dir>, hoặc khai "
        + `"apps.${args.appId || "<app_id>"}.source" trong config (--config config/nav_sources.json).`,
    );
  }
  const root = path.resolve(source);
  if (!fs.existsSync(root)) throw new Error(`Không thấy thư mục source: ${root}`);
  return {
    root,
    include: args.include.length ? args.include : (fromConfig.include || []),
    exclude: args.exclude.length ? args.exclude : (fromConfig.exclude || []),
  };
}

function babelVersionOf(sourceRoot) {
  try {
    const pkg = createRequire(path.join(sourceRoot, "package.json"))("@babel/parser/package.json");
    return pkg.version || "unknown";
  } catch {
    return "unknown";
  }
}

function main(argv) {
  const args = parseArgs(argv);
  if (args.help) {
    process.stdout.write(
      "nav_analyze.mjs — trích Navigation Graph từ source React Native (AST, không LLM)\n"
        + "  --source <dir>       thư mục project RN (bắt buộc nếu config không khai)\n"
        + "  --app-id <id>        app trong KB (tra config + ghi vào output)\n"
        + "  --config <file>      config/nav_sources.json\n"
        + "  --babel-from <dir>   project có node_modules/@babel (khi source không có)\n"
        + "  --out <file>         ghi nav-graph.json (không có thì chỉ in số liệu)\n"
        + "  --include/--exclude  glob, lặp lại được\n"
        + "  --max-files <n>      trần số file phân tích (mặc định 6000)\n"
        + "  --dry-run            không ghi file\n"
        + "  --verbose            in chi tiết từng mục\n",
    );
    return 0;
  }

  const config = args.config ? loadConfig(args.config) : { apps: {} };
  const { root, include, exclude } = resolveSource(args, config);
  const babel = loadBabel(root, args.babelFrom ? path.resolve(args.babelFrom) : "");
  const babelVersion = babelVersionOf(args.babelFrom ? path.resolve(args.babelFrom) : root);

  const { files, truncated } = walkSource(root, { include, exclude, maxFiles: args.maxFiles });

  const asts = [];
  const parseErrors = [];
  for (const file of files) {
    let code = "";
    try {
      code = fs.readFileSync(file.absolute, "utf8");
    } catch (error) {
      parseErrors.push({ file: file.rel, error: `read: ${error.message}` });
      continue;
    }
    try {
      asts.push({ file: file.rel, code, ast: parseWithFallback(babel, code, file.rel) });
    } catch (error) {
      parseErrors.push({ file: file.rel, error: `parse: ${error.message}` });
    }
  }

  const registry = { screens: [], keys: {}, navigators: [] };
  // Pass 1a trước: bảng route key phải có trước khi đọc `name={KEY_NAME_X}`.
  for (const { file, code, ast } of asts) collectKeys(babel, ast, code, file, registry);
  for (const { file, code, ast } of asts) collectRegistry(babel, ast, code, file, registry);

  const screensByComponent = {};
  for (const screen of registry.screens) {
    // Giữ đăng ký **đầu tiên**: một component có thể xuất hiện ở nhiều navigator (stack + tab),
    // và route của stack mới là màn hình thật.
    if (screen.component && !screensByComponent[screen.component]) {
      screensByComponent[screen.component] = screen.route;
    }
  }
  const componentNames = new Set(Object.keys(screensByComponent));

  const renderOut = { usage: new Map() };
  for (const { file, code, ast } of asts) collectRenderEdges(babel, ast, code, file, renderOut);

  const calls = {
    edges: [],
    unresolved: [],
    keys: registry.keys,
    screensByComponent,
    componentNames,
    usageIndex: renderOut.usage,
    registeredRoutes: new Set(registry.screens.map((screen) => screen.route.toLowerCase())),
  };
  const overlayOut = { overlays: [], usageIndex: renderOut.usage, screensByComponent };
  for (const { file, code, ast } of asts) {
    collectCallSites(babel, ast, code, file, screensByComponent, calls);
  }
  for (const { file, code, ast } of asts) {
    collectOverlays(babel, ast, code, file, screensByComponent, overlayOut);
  }
  const overlays = overlayOut.overlays;

  const graph = assemble(registry, calls, overlays, {
    root,
    app_id: args.appId,
    files_scanned: files.length,
    files_parsed: asts.length,
    truncated: Boolean(truncated),
    parse_errors: parseErrors,
    babel_version: babelVersion,
  });

  if (args.out && !args.dryRun) {
    fs.writeFileSync(args.out, `${JSON.stringify(graph, null, 2)}\n`, "utf8");
  }

  const counts = graph.counts;
  process.stdout.write(
    `source: ${root}${args.appId ? ` (app_id=${args.appId})` : ""}\n`
      + `file quét ${graph.source.files_scanned} · parse được ${graph.source.files_parsed}`
      + ` · lỗi parse ${counts.parse_errors}${truncated ? " · ĐÃ CHẠM TRẦN --max-files" : ""}\n`
      + `màn hình ${counts.screens} (route table ${counts.screens_from_route_table},`
      + ` deep link ${counts.screens_from_linking}) · route key ${counts.route_keys}`
      + ` · navigator ${counts.navigators}\n`
      + `cạnh có bằng chứng ${counts.edges_with_evidence} (có guard ${counts.edges_with_guards},`
      + ` tổng guard ${counts.guards})\n`
      + `không xác định ${counts.unresolved} ${JSON.stringify(counts.unresolved_by_kind)}\n`
      + `overlay: định nghĩa ${counts.overlays_defined} · chỗ dùng ${counts.overlays_used}`
      + ` ${JSON.stringify(counts.overlays_by_kind)}\n`
      + (args.out && !args.dryRun ? `đã ghi ${args.out}\n` : "chế độ dry-run: không ghi file\n"),
  );

  if (args.verbose) {
    for (const screen of graph.screens) {
      process.stdout.write(
        `  screen ${screen.key} ← route "${screen.route}" ${screen.components.join(",")}\n`,
      );
    }
    for (const edge of graph.edges) {
      process.stdout.write(
        `  edge ${edge.from} → ${edge.to} [${edge.kind}] guards=${edge.guards.length}`
          + ` evidence=${edge.evidence.length}\n`,
      );
    }
    for (const item of graph.unresolved) {
      process.stdout.write(`  unresolved ${item.kind} ${item.file}:${item.line}\n`);
    }
  }
  return 0;
}

const invokedDirectly = process.argv[1]
  && path.resolve(process.argv[1]) === path.resolve(new URL(import.meta.url).pathname);

if (invokedDirectly) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`nav_analyze: ${error.message}\n`);
    process.exitCode = 2;
  }
}

process.on("uncaughtException", (error) => {
  process.stderr.write(`nav_analyze: ${error.message}\n`);
  process.exit(2);
});

"use strict";

const { spawnSync } = require("node:child_process");

// 依次运行服务契约与全部领域测试。
const modules = [
  "service_contract",
  "test_audit",
  "test_identity",
  "test_sensors",
  "test_exposure",
  "test_estimates",
  "test_feedback",
  "test_httpapi",
];

for (const mod of modules) {
  const result = spawnSync("python3", ["-m", "unittest", "-v", mod], {
    stdio: "inherit",
  });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

/**
 * Protected Paths Extension
 *
 * Blocks write and edit operations to protected paths.
 * Useful for preventing accidental modifications to sensitive files.
 *
 * 来源：pi 官方扩展示例（examples/extensions/protected-paths.ts），按本工作区结构定制清单：
 * - .env / .git/ / node_modules/：官方默认
 * - projects/、workspace/：工作区相邻的真实项目区与过程区，开发仓库内的会话不得写入。
 *   注意这是防误操作护栏，拦不住 bash 直写；真实资料隔离最终靠物理分区。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	const protectedPaths = [".env", ".git/", "node_modules/", "projects/", "workspace/"];

	pi.on("tool_call", async (event, ctx) => {
		if (event.toolName !== "write" && event.toolName !== "edit") {
			return undefined;
		}

		const path = event.input.path as string;
		const isProtected = protectedPaths.some((p) => path.includes(p));

		if (isProtected) {
			if (ctx.hasUI) {
				ctx.ui.notify(`Blocked write to protected path: ${path}`, "warning");
			}
			return { block: true, reason: `Path "${path}" is protected` };
		}

		return undefined;
	});
}

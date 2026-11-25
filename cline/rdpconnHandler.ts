/**
 * Провайдер Cline: текст локального кодера через буфер, без http:// и без порта.
 *
 * Расширение уже собрано в сессии. В apps/vscode/src/sdk/sdk-api-handler.ts
 * в buildApiHandler, до вызова SDK:
 *
 *   if (providerId === "rdpconn") return new RdpconnHandler()
 *
 * В список провайдеров настроек добавить пункт rdpconn с подписью
 * «Локальный кодер (буфер)». Ключ и base URL не нужны: модель на той
 * стороне уже слушает, этот процесс только пишет JSON в stdin гостя.
 *
 * Гость: python -m rdpconn guest --feed model --peer-id B
 * Хозяин буфера (машина с моделью): python -m rdpconn host --feed model --peer-id A
 * Одновременно со шлюзом не запускать.
 */
import { ChildProcessWithoutNullStreams, spawn } from "child_process"
import * as readline from "readline"

const PYTHON = process.env.RDPCONN_PYTHON || "C:\\Users\\rdp\\py312\\python.exe"
const ROOT = process.env.RDPCONN_ROOT || "C:\\Users\\rdp\\rdp_conn"

let child: ChildProcessWithoutNullStreams | null = null
let lines: readline.Interface | null = null
const queue: string[] = []
let waiter: ((line: string) => void) | null = null

function guest(): ChildProcessWithoutNullStreams {
	if (child && !child.killed && child.exitCode === null) return child
	queue.length = 0
	const proc = spawn(PYTHON, ["-m", "rdpconn", "guest", "--feed", "model", "--peer-id", "B", "--wait", "45"], {
		cwd: ROOT,
		env: { ...process.env, PYTHONPATH: ROOT, PYTHONIOENCODING: "utf-8" },
		stdio: ["pipe", "pipe", "pipe"],
		windowsHide: true,
	})
	proc.stderr.resume()
	proc.on("exit", () => {
		// Выход предыдущего процесса не должен срывать уже новый запрос.
		if (child !== proc || !waiter) return
		const next = waiter
		waiter = null
		next(JSON.stringify({ error: "coder closed" }))
	})
	child = proc
	lines = readline.createInterface({ input: proc.stdout })
	lines.on("line", (line) => {
		if (child !== proc) return
		if (waiter) {
			const next = waiter
			waiter = null
			next(line)
			return
		}
		queue.push(line)
	})
	return child
}

function nextLine(): Promise<string> {
	const queued = queue.shift()
	if (queued !== undefined) return Promise.resolve(queued)
	return new Promise((resolve) => {
		waiter = resolve
	})
}

function textOf(content: unknown): string {
	if (typeof content === "string") return content
	if (!Array.isArray(content)) return ""
	return content
		.map((part) => {
			if (typeof part === "string") return part
			if (part && typeof part === "object" && "text" in part) return String((part as { text?: string }).text || "")
			return ""
		})
		.join("")
}

export class RdpconnHandler {
	async *createMessage(systemPrompt: string, messages: Array<{ role?: string; content?: unknown }>): AsyncGenerator<{ type: string; text?: string; reasoning?: string }> {
		const proc = guest()
		const body = JSON.stringify({
			system: systemPrompt,
			messages: messages.map((message) => ({
				role: message.role || "user",
				content: textOf(message.content),
			})),
		})
		proc.stdin.write(body + "\n")
		for (;;) {
			const line = await nextLine()
			let obj: { text?: string; reasoning?: string; done?: boolean; error?: string }
			try {
				obj = JSON.parse(line)
			} catch {
				continue
			}
			if (obj.error) throw new Error(obj.error)
			if (obj.reasoning) yield { type: "reasoning", reasoning: obj.reasoning }
			if (obj.text) yield { type: "text", text: obj.text }
			if (obj.done) return
		}
	}

	getModel() {
		return {
			id: "qwen2.5-coder-7b-instruct-q4_k_m",
			info: {
				maxTokens: 8192,
				contextWindow: 8192,
				supportsImages: false,
				supportsPromptCache: false,
			},
		}
	}
}

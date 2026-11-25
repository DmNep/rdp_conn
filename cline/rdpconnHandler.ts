/**
 * Провайдер Cline: текст локального кодера через буфер, без http:// и без порта.
 *
 * Это читаемый эталон того, что реально пропатчено в собранный .vsix —
 * не исходник, из которого расширение собирается. Патч (F:\rdp-lab\qwen\
 * patch_cline.py на стенде с моделью) правит уже скомпилированный бандл
 * целиком, без пересборки Cline из исходников:
 *
 *   1. Тело этого файла (минифицированное) вставляется в начало
 *      extension.js как `globalThis.__rdpconnHandler = function() {...}` —
 *      фабрика, не класс: диспетчер вызывает её заново на каждую
 *      инициализацию обработчика, поэтому child/queue/waiter живут внутри
 *      функции, а не на уровне модуля.
 *   2. В extension.js патчится диспетчер провайдеров (функция вида `VJe`):
 *      `if (r === "rdpconn") return globalThis.__rdpconnHandler()` —
 *      перед обычной веткой SDK, а не как случай внутри неё.
 *   3. В extension.js патчится enum провайдеров: добавляется
 *      `t.RDPCONN = "rdpconn"`.
 *   4. И в extension.js, и в webview-ui/build/assets/index.js добавляется
 *      карточка каталога провайдеров (id "rdpconn", family
 *      "openai-compatible", apiKeyOptional) — без неё провайдер не
 *      появится в списке настроек. Ключ и base URL не нужны: модель на
 *      той стороне уже слушает, этот процесс только пишет JSON в stdin
 *      гостя.
 *
 * Гость: python -m rdpconn guest --feed model --peer-id B
 * Хозяин буфера (машина с моделью): python -m rdpconn host --feed model --peer-id A
 * Одновременно со шлюзом не запускать.
 *
 * Пути к python и к rdp_conn в реально развёрнутом патче зашиты жёстко
 * (C:\Users\rdp\...) — переменные окружения ниже это не меняет, а просто
 * оставляет путь к настройке на случай другого стенда; на известном
 * стенде оба значения совпадают.
 */
import { ChildProcessWithoutNullStreams, spawn } from "child_process"
import * as readline from "readline"

const PYTHON = process.env.RDPCONN_PYTHON || "C:\\Users\\rdp\\py312\\python.exe"
const ROOT = process.env.RDPCONN_ROOT || "C:\\Users\\rdp\\rdp_conn"

type Message = { role?: string; content?: unknown }
type Chunk = { type: string; text?: string; reasoning?: string }

function textOf(content: unknown): string {
	if (typeof content === "string") return content
	if (Array.isArray(content)) {
		return content
			.map((part) => {
				if (typeof part === "string") return part
				if (part && typeof part === "object" && "text" in part) return String((part as { text?: string }).text || "")
				return ""
			})
			.join("")
	}
	// Одиночный объект с .text — случай, который реально встречается в
	// содержимом сообщения Cline, не только строка и массив частей.
	if (content && typeof content === "object" && "text" in (content as { text?: unknown })) {
		return String((content as { text?: string }).text || "")
	}
	return ""
}

/**
 * Фабрика, не класс: диспетчер (см. шапку файла) вызывает её заново на
 * каждую инициализацию обработчика, поэтому состояние гостевого процесса
 * живёт в замыкании одного вызова, а не на уровне модуля — два параллельных
 * обработчика (если Cline их когда-нибудь создаст) не делят один child.
 */
export function createRdpconnHandler() {
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

	return {
		async *createMessage(systemPrompt: string, messages: Message[]): AsyncGenerator<Chunk> {
			const proc = guest()
			const body = JSON.stringify({
				system: systemPrompt || "",
				messages: (messages || []).map((message) => ({
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
				// text: obj.reasoning дублирует рассуждение в то же поле, что
				// обычный текст: часть Cline показывает/копит только text,
				// и без дубля рассуждение выпало бы из накопленного ответа.
				if (obj.reasoning) yield { type: "reasoning", reasoning: obj.reasoning, text: obj.reasoning }
				if (obj.text) yield { type: "text", text: obj.text }
				if (obj.done) return
			}
		},

		getModel() {
			return {
				id: "qwen2.5-coder-7b-instruct-q4_k_m",
				info: {
					maxTokens: 2048,
					contextWindow: 8192,
					supportsImages: false,
					supportsPromptCache: false,
					inputPrice: 0,
					outputPrice: 0,
				},
			}
		},

		// Пустые, но обязаны существовать: Cline зовёт их у любого обработчика
		// при отмене хода, обработчик без них роняет это место с исключением.
		abort() {},
		setAbortSignal() {},
	}
}

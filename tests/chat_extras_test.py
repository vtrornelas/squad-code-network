"""Chat extras with the real bridge and the fake Claude (no API cost): file attachments, reading the
operation folder (opt-in) and voice dictation.

Run from the repository root (needs `npm install`, Playwright for Python and Chrome or Chromium):
    python tests/chat_extras_test.py
The bridge starts on a free port with temporary data and projects folders, so nothing of yours is touched.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright
import json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request

ROOT = Path(__file__).resolve().parents[1]
results = []

def test(name, condition=True):
    assert condition, name
    results.append(name)
    print('PASS', name, flush=True)

def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p

# The browser has no microphone here: a stand-in SpeechRecognition records start/stop and lets the test speak.
FAKE_SPEECH = """
window.__speech = [];
class FakeRecognition {
  constructor() { this.lang = ''; this.continuous = false; this.interimResults = false; window.__speech.push(this); }
  start() { this.started = true; }
  stop() { this.stopped = true; setTimeout(() => this.onend && this.onend(), 0); }
  abort() { this.stop(); }
  say(text, final) {
    const r = [{ transcript: text }]; r.isFinal = !!final;
    this.onresult && this.onresult({ resultIndex: 0, results: [r] });
  }
}
window.SpeechRecognition = FakeRecognition; window.webkitSpeechRecognition = FakeRecognition;
"""

tmp = Path(tempfile.mkdtemp(prefix='squad-chat-'))
fake = ROOT / 'tests' / 'fake-claude.mjs'
fake_mode = fake.stat().st_mode
os.chmod(fake, fake_mode | 0o111)  # the bridge runs it as `claude`; restored at the end
port = free_port()
env = {**os.environ, 'SQUAD_PORT': str(port), 'SQUAD_CLAUDE_BIN': str(fake),
       'SQUAD_DATA_DIR': str(tmp / 'data'), 'SQUAD_PROJECTS_DIR': str(tmp / 'projects')}
bridge = subprocess.Popen(['node', 'server.js'], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f'http://127.0.0.1:{port}'
for _ in range(100):
    try: urllib.request.urlopen(base + '/', timeout=1); break
    except Exception: time.sleep(0.1)

doc = tmp / 'spec-nos-em-numeros.md'
doc.write_text('# Spec\n\nSEGREDO-DO-ARQUIVO: nada sai do navegador.\n', encoding='utf-8')
runs = []

def last_run():
    return runs[-1] if runs else None

def send_and_wait(page, text=''):
    before = len(runs)
    if text: page.fill('#feChatInput', text)
    page.click('[data-action="fe-send"]')
    page.wait_for_function(f'document.querySelectorAll("#feMsgs .fe-row.agent").length >= 1 && !chatNow().busy', timeout=15000)
    page.wait_for_timeout(100)
    assert len(runs) > before, 'nenhuma execução enviada ao bridge'
    return last_run()

try:
    with sync_playwright() as pw:
        binary = os.environ.get('CHROMIUM_EXECUTABLE') or shutil.which('chromium') or shutil.which('google-chrome')
        browser = pw.chromium.launch(executable_path=binary, headless=True)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: runs.append(json.loads(r.post_data)) if r.method == 'POST' and r.url.endswith('/api/runs') else None)
        page.add_init_script(FAKE_SPEECH)
        page.goto(base + '/')
        page.click('[data-action="db-setup-demo"]')
        page.wait_for_function('diskSync.on', timeout=10000)
        cmd = page.evaluate('project().commanderId')
        page.evaluate(f'openAgentChat("{cmd}")')
        page.wait_for_selector('#feChatInput')

        # 1. Attachments
        test('O chat tem botão de anexar', page.locator('[data-action="fe-attach"]').count() == 1)
        page.set_input_files('#feAttachInput', str(doc))
        page.wait_for_selector('#feAttach .fe-file')
        test('O arquivo escolhido aparece como chip antes de enviar', 'spec-nos-em-numeros.md' in page.inner_text('#feAttach'))
        test('Só com anexo, o botão de enviar fica ativo', not page.locator('.fe-send').is_disabled())
        run = send_and_wait(page)
        test('O conteúdo do arquivo chega ao agente', 'SEGREDO-DO-ARQUIVO' in run['prompt'])
        test('A mensagem enviada mostra o arquivo', page.locator('#feMsgs .fe-row.user .fe-file').count() == 1)
        test('Depois de enviar, a fila de anexos fica vazia', page.locator('#feAttach .fe-file').count() == 0)
        run = send_and_wait(page, 'E agora, ainda lembra do arquivo?')
        test('O arquivo continua no contexto nas mensagens seguintes', 'SEGREDO-DO-ARQUIVO' in run['prompt'])
        test('Sem a opção ligada, o chat continua sem ferramentas', run['options'].get('tools') == [])

        # 2. Reading the operation folder (opt-in)
        page.evaluate('closeModal()')
        page.evaluate('openSettings("claude")')
        page.wait_for_selector('#runtimeForm [name="chatRead"]')
        test('A opção de leitura da pasta vem desligada', not page.locator('#runtimeForm [name="chatRead"]').is_checked())
        page.check('#runtimeForm [name="chatRead"]')
        page.evaluate('saveRuntime(document.getElementById("runtimeForm"))')
        page.wait_for_function('runtime().chatRead===true', timeout=5000)
        # Saving re-checks the bridge and then reopens the settings: wait for it, or it would cover the chat opened next.
        page.wait_for_function('!!document.querySelector("#runtimeForm") && ui.settingsTab==="claude"', timeout=5000)
        page.wait_for_timeout(1500)
        page.evaluate('closeModal()')
        page.evaluate(f'openAgentChat("{cmd}")')
        page.wait_for_selector('#feChatInput')
        run = send_and_wait(page, 'Leia o README da pasta')
        tools = run['options'].get('tools') or []
        test('Com a opção ligada, o chat recebe só ferramentas de leitura', sorted(tools) == ['Glob', 'Grep', 'Read'])
        test('As ferramentas de leitura vêm liberadas', sorted(run['options'].get('allowedTools') or []) == ['Glob', 'Grep', 'Read'])
        test('O agente é avisado de que pode ler a pasta da operação', 'pasta da operação' in run['systemPrompt'])
        test('A configuração sobrevive a uma recarga', page.evaluate('normalizeRuntime(JSON.parse(JSON.stringify(state.settings.runtime))).chatRead') is True)

        # 3. Voice dictation
        test('O chat tem botão de microfone', page.locator('[data-action="fe-mic"]').count() == 1)
        page.fill('#feChatInput', 'Oi')
        page.click('[data-action="fe-mic"]')
        test('O ditado começa em português do Brasil', page.evaluate('__speech.at(-1).started && __speech.at(-1).lang') == 'pt-BR')
        test('O botão mostra que está gravando', 'on' in page.get_attribute('[data-action="fe-mic"]', 'class'))
        page.evaluate('__speech.at(-1).say("tudo bem com você", true)')
        test('A fala entra na caixa, depois do que já estava escrito', page.input_value('#feChatInput') == 'Oi tudo bem com você')
        test('Com texto ditado, dá para enviar', not page.locator('.fe-send').is_disabled())
        page.click('[data-action="fe-mic"]')
        page.wait_for_function('!document.querySelector("[data-action=fe-mic]").classList.contains("on")', timeout=3000)
        test('Clicar de novo para o ditado', page.evaluate('__speech.at(-1).stopped') is True)

        # 4. Same tools in the feature co-writing chat
        page.evaluate('closeModal()')
        page.evaluate('openFeatureEditor()')
        page.wait_for_selector('#feChatInput')
        test('A co-escrita de feature também tem anexo e microfone',
             page.locator('[data-action="fe-attach"]').count() == 1 and page.locator('[data-action="fe-mic"]').count() == 1)

        # 5. Attached files are saved with the conversation
        page.evaluate('closeModal()')
        page.wait_for_timeout(1200)
        page.reload()
        page.wait_for_function('diskSync.on', timeout=10000)
        page.evaluate(f'openAgentChat("{cmd}")')
        page.wait_for_selector('#feChatInput')
        run = send_and_wait(page, 'Depois de recarregar, o arquivo ainda está aí?')
        test('Depois de recarregar a página, o arquivo continua na conversa', 'SEGREDO-DO-ARQUIVO' in run['prompt'])
        page.set_input_files('#feAttachInput', str(doc))
        page.wait_for_selector('#feAttach .fe-file')
        page.fill('#feChatInput', 'Segue a versão nova da spec')
        page.click('[data-action="fe-mic"]')
        page.wait_for_timeout(500)
        test('Gravando, o microfone fica vermelho', page.evaluate('getComputedStyle(document.querySelector("[data-action=fe-mic]")).backgroundColor').replace('rgba', 'rgb').replace(', 1)', ')') == 'rgb(214, 69, 61)')  # Chrome may report rgba(..., 1) right after the transition
        page.screenshot(path=os.environ.get('SQUAD_PREVIEW', str(tmp / 'chat-extras.png')), clip={'x': 0, 'y': 0, 'width': 1500, 'height': 950})
        page.click('[data-action="fe-mic"]')

        test('Nenhum erro de JavaScript na página', not errors)
        browser.close()
finally:
    bridge.terminate()
    try: bridge.wait(5)
    except Exception: bridge.kill()
    shutil.rmtree(tmp, ignore_errors=True)
    os.chmod(fake, fake_mode)

print(f'\n{len(results)} testes passaram.')

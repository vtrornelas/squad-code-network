"""Sprint proposals in the chats, with the real bridge and the fake Claude (no API cost).

The commander (agent chat) and the hosts of Agent Teams can end a reply with a ```json "sprint" block; the chat shows it as a
card with "Criar e iniciar" / "Só criar", which creates the sprint and its features in the operation.
Run from the repository root (needs `npm install`, Playwright for Python and Chrome or Chromium):
    python tests/sprint_proposal_test.py
"""
from pathlib import Path
from playwright.sync_api import sync_playwright
import json, os, shutil, socket, subprocess, tempfile, time, urllib.request

ROOT = Path(__file__).resolve().parents[1]
results = []

def test(name, condition=True):
    assert condition, name
    results.append(name)
    print('PASS', name, flush=True)

def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p

tmp = Path(tempfile.mkdtemp(prefix='squad-sprint-'))
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
runs = []

def send_and_wait(page, text):
    before = len(runs)
    page.fill('#feChatInput', text)
    page.click('[data-action="fe-send"]')
    page.wait_for_function('chatNow() && !chatNow().busy && chatNow().messages.at(-1).role!=="user"', timeout=20000)
    page.wait_for_timeout(200)
    assert len(runs) > before, 'nenhuma execução enviada ao bridge'
    return runs[-1]

try:
    with sync_playwright() as pw:
        binary = os.environ.get('CHROMIUM_EXECUTABLE') or shutil.which('chromium') or shutil.which('google-chrome')
        browser = pw.chromium.launch(executable_path=binary, headless=True)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: runs.append(json.loads(r.post_data)) if r.method == 'POST' and r.url.endswith('/api/runs') else None)
        page.goto(base + '/')
        page.click('[data-action="db-setup-demo"]')
        page.wait_for_function('diskSync.on', timeout=10000)
        sprints0 = page.evaluate('project().sprints.length')
        feats0 = page.evaluate('project().features.length')
        cmd = page.evaluate('project().commanderId')
        op = page.evaluate('project().agentIds.find(id=>agentById(id).role!=="commander"&&chatFamily(agentById(id))==="op")')

        # 1. The commander is told how to propose a sprint; an operator is not.
        page.evaluate(f'openAgentChat("{cmd}")'); page.wait_for_selector('#feChatInput')
        run = send_and_wait(page, 'Vamos planejar a próxima sprint?')
        test('O comandante recebe as instruções para propor uma sprint', 'Propor uma nova sprint' in run['systemPrompt'])
        page.evaluate('closeModal()')
        page.evaluate(f'openAgentChat("{op}")'); page.wait_for_selector('#feChatInput')
        run = send_and_wait(page, 'Oi')
        test('Um operador não recebe essas instruções', 'Propor uma nova sprint' not in run['systemPrompt'])
        page.evaluate('closeModal()')

        # 2. A reply with a "sprint" block becomes a card, not text to copy.
        page.evaluate(f'openAgentChat("{cmd}")'); page.wait_for_selector('#feChatInput')
        send_and_wait(page, 'FAKESPRINT monta a sprint 02')
        test('A proposta aparece como cartão no chat', page.locator('#feMsgs .sp-card').count() == 1)
        card = page.inner_text('#feMsgs .sp-card')
        test('O cartão mostra o nome, o objetivo e as features', 'Sprint 02' in card and 'Resultado em stories' in card and 'Telas de stories' in card and 'Card por tela' in card)
        bubble = page.inner_text('#feMsgs .fe-row.agent:has(.sp-card) .fe-bubble')
        test('O JSON não aparece no texto da mensagem, só o resumo', bubble.strip() == 'Montei a Sprint 02 com duas features.')
        test('O cartão tem os botões Criar e iniciar e Só criar',
             page.locator('.sp-card [data-action="sprint-accept"][data-start="1"]').count() == 1 and page.locator('.sp-card [data-action="sprint-accept"]:not([data-start])').count() == 1)

        # 3. "Só criar" creates the sprint and its features in the operation.
        page.click('.sp-card [data-action="sprint-accept"]:not([data-start])')
        page.wait_for_function(f'project().sprints.length === {sprints0 + 1}', timeout=5000)
        s = page.evaluate('(()=>{const p=project(),s=p.sprints.at(-1);return{name:s.name,goal:s.goal,fs:sprintFeatures(s,p).map(f=>({key:f.key,title:f.title,scope:f.scope,priority:f.priority,criteria:f.criteria,description:f.description,tasks:f.tasks,deps:f.dependencies.map(d=>featureById(d)?.key)}))}})()')
        test('A sprint é criada com o nome e o objetivo da proposta', s['name'] == 'Sprint 02' and s['goal'] == 'Resultado em stories, uma métrica por tela.')
        test('As duas features entram na nova sprint', [f['title'] for f in s['fs']] == ['Telas de stories', 'Card por tela'])
        test('Escopo e prioridade vêm da proposta', s['fs'][0]['scope'] == 'frontend' and s['fs'][0]['priority'] == 'P0' and s['fs'][1]['priority'] == 'P1')
        test('Critérios de aceitação viram lista', s['fs'][0]['criteria'] == '- Passa de tela com um toque\n- Funciona no celular')
        test('Contexto, entregáveis e riscos ficam na descrição', all(x in s['fs'][0]['description'] for x in ['### Contexto', '### Entregáveis', '### Riscos', 'Telas lentas']))
        test('As tarefas vêm como lista', s['fs'][0]['tasks'] == '- Componente de tela\n- Navegação por toque')
        test('As features ganham códigos novos, depois dos que existiam', all(f['key'] not in [x['key'] for x in s['fs'] if x is not f] for f in s['fs']) and page.evaluate('project().features.length') == feats0 + 2)
        test('Dependências por código e por título são ligadas, além do setup', s['fs'][0]['deps'][:2] == ['F00', 'F01'] and s['fs'][1]['deps'] == ['F00', s['fs'][0]['key']])
        test('Depois de criar, o cartão mostra que a sprint foi criada e sai dos botões',
             'criada' in page.inner_text('#feMsgs .sp-card') and page.locator('.sp-card [data-action="sprint-accept"]').count() == 0)
        test('O chat avisa quais features foram criadas', s['fs'][0]['key'] in page.locator('#feMsgs .fe-row.system').last.inner_text())
        page.evaluate('closeModal()'); page.wait_for_timeout(1200)
        page.reload(); page.wait_for_function('diskSync.on', timeout=10000)
        test('A sprint criada continua lá depois de recarregar', page.evaluate(f'project().sprints.length') == sprints0 + 1)
        page.evaluate(f'openAgentChat("{cmd}")'); page.wait_for_selector('#feChatInput')
        test('O cartão continua marcado como criado depois de recarregar', page.locator('.sp-card [data-action="sprint-accept"]').count() == 0 and 'criada' in page.inner_text('#feMsgs .sp-card'))

        # 4. A newer proposal replaces the buttons of the older one; "Criar e iniciar" hands the sprint to the commander.
        send_and_wait(page, 'FAKESPRINT agora a sprint 03')
        test('Uma proposta nova traz os botões de novo', page.locator('.sp-card [data-action="sprint-accept"]').count() == 2)
        page.click('.sp-card [data-action="sprint-accept"][data-start="1"]')
        page.wait_for_function(f'project().sprints.length === {sprints0 + 2}', timeout=5000)
        page.wait_for_function('!!runner || !!document.querySelector(".toast")', timeout=8000)
        state = page.evaluate('({modal:ui.modal, runner:!!runner, sprint:runner?.sprintId===project().sprints.at(-1).id, phase:runner?.phase, toast:document.querySelector(".toast")?.innerText||""})')
        test('Criar e iniciar fecha o chat e entrega a sprint ao comandante para planejar', state['modal'] != 'agent-chat' and state['runner'] and state['sprint'] and state['phase'] in ('planning', 'awaiting'))
        page.evaluate('stopRun()')

        # 5. Agent Teams: the hosts get the same instructions, and the card works there too.
        page.evaluate('goView("projects")'); page.wait_for_timeout(400)
        page.evaluate('openAgentTeams()'); page.wait_for_selector('#feChatInput', timeout=10000)
        page.wait_for_function('chatNow() && !chatNow().busy', timeout=20000)
        run = send_and_wait(page, 'FAKESPRINT bora para a sprint 04')
        test('No Agent Teams, quem conduz recebe as instruções de sprint', 'Propor uma nova sprint' in run['systemPrompt'])
        test('No Agent Teams, a lista de features mostra o status de cada uma', 'Concluída' in run['prompt'] or 'Backlog' in run['prompt'])
        page.wait_for_selector('#feMsgs .sp-card [data-action="sprint-accept"]', timeout=10000)
        page.click('#feMsgs .sp-card [data-action="sprint-accept"]:not([data-start])')
        page.wait_for_function(f'project().sprints.length === {sprints0 + 3}', timeout=5000)
        test('No Agent Teams, Só criar também cria a sprint com as features', page.evaluate('sprintFeatures(project().sprints.at(-1),project()).length') == 2)

        # Leaving the meetup to start: documents edited and not saved yet are saved first, not lost.
        page.wait_for_function('chatNow() && !chatNow().busy', timeout=20000)
        page.evaluate('(()=>{const el=document.getElementById(FE_FIELD_ID.briefing);el.value=el.value+"\\nNOTA-DA-REUNIAO";el.dispatchEvent(new Event("input",{bubbles:true}));})()')
        test('Com o briefing editado na reunião, há documento não salvo', page.evaluate('teamsDirty()') is True)
        send_and_wait(page, 'FAKESPRINT e a sprint 05')
        page.wait_for_selector('#feMsgs .sp-card [data-action="sprint-accept"][data-start="1"]', timeout=10000)
        page.click('#feMsgs .sp-card [data-action="sprint-accept"][data-start="1"]')
        page.wait_for_function(f'project().sprints.length === {sprints0 + 4}', timeout=5000)
        page.wait_for_function('ui.modal!=="teams"', timeout=8000)
        page.wait_for_timeout(800)
        test('Criar e iniciar na reunião salva os documentos antes de sair', 'NOTA-DA-REUNIAO' in page.evaluate('project().briefing'))
        test('E entrega a sprint ao comandante', page.evaluate('!!runner && runner.sprintId===project().sprints.at(-1).id'))
        page.evaluate('stopRun()')

        test('Nenhum erro de JavaScript na página', not errors)
        browser.close()
finally:
    bridge.terminate()
    try: bridge.wait(5)
    except Exception: bridge.kill()
    shutil.rmtree(tmp, ignore_errors=True)
    os.chmod(fake, fake_mode)

print(f'\n{len(results)} testes passaram.')

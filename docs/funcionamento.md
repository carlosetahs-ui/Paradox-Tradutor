# Como o tradutor funciona

Consulte o [guia de instalação e uso](../README.md) para começar.

Traduz a pasta `localisation` de **mods selecionados** do Crusader Kings II para português do Brasil,
usando um modelo de IA local (ollama/openai-compat), tradução offline (argos) ou serviços online (translators/google).
Python 3.11+; o núcleo só usa a biblioteca padrão (cada backend pede o seu `pip install`).

## Estratégia

**1. O CK2 não tem slot de português.** Os arquivos têm colunas fixas
(`#CODE;ENGLISH;FRENCH;GERMAN;;SPANISH;;;;;;;;;x`). O português é gravado na(s) coluna(s) que você
escolher em `write_columns` (padrão: `english`), e o jogo precisa estar no idioma dessa coluna.
Para o mod ficar em português em qualquer idioma do jogo, use
`write_columns = ["english", "french", "german", "spanish"]`.
Os índices das colunas são lidos do cabeçalho `#CODE;...` de cada arquivo.

**2. Mod de tradução separado (`overlay`, padrão).** Para cada mod, o programa cria
`saida/<mod>_ptbr/localisation/` com os arquivos **com o mesmo nome** dos originais, mais um
`<mod>_ptbr.mod` com `dependencies = { "<nome do mod original>" }`. Pela wiki do CK2, um arquivo de mod
com o mesmo nome substitui o original, e o `dependencies` faz o mod de tradução carregar depois.
O mod original nunca é alterado. (A wiki também diz que, para chaves duplicadas em arquivos diferentes,
o primeiro arquivo carregado vence, por isso a estratégia não depende de chaves duplicadas.)
O modo `inplace` sobrescreve o mod, mas faz backup em `saida/backup/` e se recusa a rodar de novo,
para nunca "traduzir português para português".

**3. Preservação dos CSVs.** Linhas não traduzidas voltam byte a byte idênticas. São mantidos: encoding
(Windows-1252, UTF-8 ou UTF-8 com BOM), `\r\n` ou `\n` por linha, comentários, linhas vazias, cabeçalho,
coluna final `x` e as outras colunas de idioma. O comando `verify` confere isso arquivo por arquivo.

**4. Códigos do jogo protegidos.** `$VAR$`, `[Root.GetName]`, `§Y ... §!`, `£gold£`, `\n` e `{...}` viram
`{1}`, `{2}`... antes de ir ao modelo e voltam depois. Se o modelo perder, repetir ou inventar códigos,
a resposta é rejeitada, o modelo é avisado e tenta de novo (3 tentativas); se falhar, a linha **fica em
inglês** e entra em `saida/<mod>_relatorio.csv`. Também são tratados: `;` na tradução (vira `,`),
quebras de linha reais, `<think>`, cercas de código e caracteres fora do Windows-1252.

**4b. Trava de segurança (`safety`).** O tradutor só altera a coluna de texto dos `.csv`; nunca toca nos scripts do mod.
Com `safety = "strict"` (padrão), antes de gravar cada linha ela é comparada com o original: se a quantidade de
`[ ] $ § £ { } \\` mudar, se os códigos do jogo forem diferentes, se os espaços das pontas mudarem ou houver
caracteres de controle, a linha **volta ao inglês** e entra no relatório como `revertida`. Aspas e travessões
"tipográficos" que o original não usava viram ASCII. `safety = "paranoid"` também não traduz nenhuma linha que tenha
códigos do jogo. O `verify` aplica as mesmas regras.

**5. Cache, keep e glossário.** Cada tradução vai para `cache.sqlite`; se interromper (Ctrl+C), é só rodar de
novo. Textos repetidos são traduzidos uma vez. Os termos ficam em dois arquivos de texto ao lado do `config.toml`
(mudar um termo refaz só as linhas que o contêm):

- `keep.txt`: nomes que **não** se traduzem, um por linha (`Winterfell`, `Night's Watch`).
- `glossary.txt`: traduções fixas, uma por linha: `Maester = Meistre`. Também aceita `->` ou `:`.
- Linhas vazias e as que começam com `#` são ignoradas. Arquivos em UTF-8. Sem `;` na tradução.
- Nomes diferentes ou vários arquivos: `keep_file` / `glossary_file` em `[translate]` (texto ou lista). `""` desliga.
- Os termos escritos direto em `[keep]` / `[glossary]` do `config.toml` continuam valendo e são somados aos arquivos.
- Casam sem diferenciar maiúsculas/minúsculas e só como palavra inteira (`King` não pega `Kingdom` nem `Kings`).
- Se o termo estiver capitalizado no original ("The King") e a tradução começar em minúscula, a inicial sai maiúscula.

Como cada backend usa os termos: `ollama`/`openai` recebem no prompt ("manter exatamente" / "traduza X como Y").
`translators`, `argos` e `google` não entendem instruções, então o termo vira um marcador `{n}` (como os códigos do
jogo) e **a tradução do glossário é colocada no lugar depois** — é imposição exata, sem depender do serviço.
Cuidado: por isso um erro no glossário se repete em todas as linhas; revise antes de ativar.

`python glossario_do_cache.py` lê o `cache.sqlite` e grava `saida/keep_sugerido.txt` e `saida/glossary_sugerido.txt`
(glossário todo comentado). Revise, ative o que estiver certo e salve como `keep.txt` / `glossary.txt`.

**6. Trocar de backend sem perder trabalho.** O cache guarda cada tradução com o nome do backend
(`ollama:<modelo>`, `google`, `argos`). Com `reuse_cache_from = "auto"` (padrão), o backend atual reaproveita o que
QUALQUER outro já traduziu e só traduz o que falta, nos dois sentidos. Entradas antigas com defeito (marcadores
perdidos, chinês no meio do texto) são ignoradas e refeitas. Para dar prioridade, use uma lista:
`reuse_cache_from = ["google", "argos", "ollama"]` (a primeira que tiver o texto vence); `[]` desliga.

**7. Backends.** `ollama`/`openai` (modelo local, lento em CPU), `google` (rápido, envia o texto ao Google, pode
bloquear por IP) e `argos` (tradução automática offline, sem limite: `pip install argostranslate` e, uma vez,
`python ck2tr.py argos-setup`).

**8. Backend `translators` (recomendado).** `pip install translators`. Usa vários serviços online
(google, bing, alibaba, yandex) em rodízio, com proteção contra bloqueio por excesso de pedidos:
limite por minuto e por hora, pausas aleatórias entre pedidos (parece humano), pausa longa a cada N
pedidos, backoff exponencial com variação aleatória, e se um serviço bloquear (429/403/captcha) ele fica
em pausa (`cooldown`) enquanto o próximo assume. Se todos bloquearem, o programa espera; o cache garante
que nada se perde. Ajuste tudo em `[model]` no `config.toml`.
Proteção dos códigos em 3 níveis: (A) frase inteira com marcadores, consertando variações que os serviços criam
(`xx 1 XX`, X cirílico...); (B) se algum código sumir, repetir ou sobrar, tenta outro serviço; (C) se ainda falhar,
traduz só os trechos ENTRE os códigos, que assim nunca passam pelo serviço. Depois disso a validação normal ainda roda.

**9. Pós-edição (pt-PT -> pt-BR e correções suas).** Os serviços online misturam português de Portugal
("partilhar", "ecrã", "está a fazer", "impressionam-me"). Depois de traduzir, o programa corrige o vocabulário, as
grafias antigas, o gerúndio ("está a fazer" -> "está fazendo") e a posição do pronome ("me impressionam"). Dá para
desligar com `ptbr_fix = false`. Você também pode listar correções em `substituicoes.txt` (`errado = certo`).
Isso roda SOBRE o texto com marcadores `{n}`, então códigos do jogo, `keep` e glossário nunca são tocados, e **não
entra no cache**: mudar as regras vale na hora, sem retraduzir nada.

## Uso

```
1. Escolha o backend no config.toml (backend = "translators", "argos", "google" ou "ollama") e instale o que ele pede
2. Copie config.example.toml para config.toml e preencha [[mods]] (name = nome EXATO do .mod original)
3. python ck2tr.py scan      --config config.toml            # analisa, sem IA e sem gravar nada (avisa arquivos repetidos entre mods)
4. python ck2tr.py status    --config config.toml            # quanto já está no cache, quanto falta e estimativa de tempo
5. python ck2tr.py translate --config config.toml --limit 30 # teste pequeno: leia o resultado!
6. python ck2tr.py translate --config config.toml            # traduz tudo (retomável)
7. python ck2tr.py verify    --config config.toml            # confere original x traduzido
8. python ck2tr.py package   --config config.toml            # verifica e gera saida/<mod>_ptbr.zip com LEIA-ME
9. Ou copie saida/<mod>_ptbr/ e saida/<mod>_ptbr.mod para a pasta mod do CK2 e ative DEPOIS do mod original
```

Comandos de apoio: `terms` (sugere nomes para o keep a partir dos textos), `diff` (original x traduzido; com
`--find sacrific` mostra as linhas que contêm o texto, sem `--find` só as que têm aviso), `argos-setup`.
`python glossario_do_cache.py` sugere `keep.txt` e `glossary.txt` a partir do cache já traduzido.
Outras opções: `--mod trecho` (só um mod), `--backend echo` (testa o pipeline sem IA), `--mode inplace`.
Caminhos relativos em `[[mods]]` valem a partir da pasta do `config.toml`. Chaves com erro de digitação em
`[translate]` geram um aviso. LM Studio e llama.cpp server funcionam com `backend = "openai"`.
Testes: `python -m unittest discover -v`, a partir da raiz do projeto.

O comando `status` lê o cache sem gravar arquivos nem carregar bibliotecas opcionais de tradução.
`diff` examina todas as colunas de destino configuradas. `verify` retorna erro quando não há arquivos
para conferir, e `package` recusa traduções com falhas estruturais. Os CSVs, descritores, relatórios e
ZIPs são gravados por substituição atômica. Linhas sem as colunas de destino ficam intactas e entram
no relatório; o programa não acrescenta colunas ao CSV.

**Se o jogo cair só com a tradução ativa:** rode `diff` (e `diff --find <trecho do evento>`), olhe `logs\error.log`
do CK2, e tente `safety = "paranoid"`. Para achar o arquivo culpado, tire arquivos de
`saida\<mod>_ptbr\localisation` aos poucos até parar de cair.

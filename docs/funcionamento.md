# Como o tradutor funciona

Consulte o [guia de instalação e uso](../README.md) para começar.

Traduz a pasta `localisation` de **mods selecionados** do Crusader Kings II ou Europa Universalis IV para português do Brasil,
usando um modelo de IA local (ollama/openai-compat), tradução offline (argos) ou serviços online (translators/google).
Python 3.11+; o núcleo só usa a biblioteca padrão (cada backend pede o seu `pip install`).

## Estratégia

**1. Perfis por jogo.** `[game] id = "ck2"` seleciona CK2, e `id = "eu4"` seleciona EU4.
Sem essa seção, o comportamento permanece CK2: configurações existentes e a entrada `ck2tr.py`
continuam funcionando. Cada execução usa um perfil para os mods listados.

**1a. Idiomas do CK2.** O CK2 não tem slot de português. Os arquivos têm colunas fixas
(`#CODE;ENGLISH;FRENCH;GERMAN;;SPANISH;;;;;;;;;x`). O português é gravado na(s) coluna(s) que você
escolher em `write_columns` (padrão: `english`), e o jogo precisa estar no idioma dessa coluna.
Para o mod ficar em português em qualquer idioma do jogo, use
`write_columns = ["english", "french", "german", "spanish"]`.
Os índices das colunas são lidos do cabeçalho `#CODE;...` de cada arquivo.

**1b. Idiomas do EU4.** Os `.yml` usam um cabeçalho como `l_english:` e entradas como
`chave:0 "Texto $VAR$" # comentário`. O programa lê `localisation` recursivamente, incluindo
subpastas como `replace`, e traduz apenas os valores do idioma escolhido em `source_column`.
Use `write_columns = [source_column]`: os identificadores devem coincidir, por exemplo
`source_column = "english"` e `write_columns = ["english"]`. Cabeçalho e nome de arquivo não são
renomeados, e a tradução PT-BR ocupa esse idioma nativo. Selecione inglês no EU4 para o padrão;
não crie um cabeçalho `l_portuguese:`.

**2. Mod de tradução separado (`overlay`, padrão).** Para cada mod, o programa cria
`saida/<mod>_ptbr/localisation/` com os arquivos **com o mesmo nome** dos originais, mais um
`<mod>_ptbr.mod` com `dependencies = { "<nome do mod original>" }`. Pela wiki do CK2, um arquivo de mod
com o mesmo nome substitui o original, e o `dependencies` faz o mod de tradução carregar depois.
O mod original nunca é alterado. (A wiki também diz que, para chaves duplicadas em arquivos diferentes,
o primeiro arquivo carregado vence, por isso a estratégia não depende de chaves duplicadas.)
No EU4, os caminhos relativos dentro de `localisation` também são mantidos. A saída inclui
`<mod>_ptbr.mod` externo e `<mod>_ptbr/descriptor.mod` interno, ambos com nome, tag `Translation`
e dependência do mod original. Apenas o descritor externo contém `path`. O campo opcional
`[game] supported_version` é escrito nos dois arquivos quando configurado; indique uma versão que
você verificou. Não se reutilizam IDs de publicação (`remote_file_id`) do original.
O nome da dependência vem de `mods.name`, de `mods.mod_file` ou, no EU4, de `descriptor.mod` do
original. Confira o nome exato para que a dependência funcione no launcher.

O EU4 aceita somente `overlay`, inclusive se você tentar sobrescrever o modo pela linha de comando.
No CK2, o modo `inplace` sobrescreve o mod, mas faz backup em `saida/backup/` e se recusa a rodar de novo,
para nunca "traduzir português para português".

**3. Preservação dos CSVs.** Linhas não traduzidas voltam byte a byte idênticas. São mantidos: encoding
(Windows-1252, UTF-8 ou UTF-8 com BOM), `\r\n` ou `\n` por linha, comentários, linhas vazias, cabeçalho,
coluna final `x` e as outras colunas de idioma. O comando `verify` confere isso arquivo por arquivo.

**3b. Preservação dos YMLs EU4.** O parser trata o dialeto de localisation Paradox diretamente,
sem serializar como YAML genérico. Preserva chave, versão numérica, cabeçalho, indentação,
comentários completos e ao fim da linha, espaços, aspas e escapes literais, além do fim de linha
de cada linha e da ausência de quebra final. A escrita é sempre UTF-8 com BOM; a leitura aceita
UTF-8 com ou sem BOM e recusa dados que não sejam UTF-8. Erros de formato são informados com
arquivo e linha. Uma linha não traduzida mantém seu conteúdo; o BOM é acrescentado se faltava.

**4. Códigos do jogo protegidos.** `$VAR$`, `[Root.GetName]`, `§Y ... §!`, `£gold£`, `\n` e `{...}` viram
`{1}`, `{2}`... antes de ir ao modelo e voltam depois. Se o modelo perder, repetir ou inventar códigos,
a resposta é rejeitada, o modelo é avisado e tenta de novo (3 tentativas); se falhar, a linha **fica em
idioma original** e entra em `saida/<mod>_relatorio.csv`. Também são tratados: `;` no CSV CK2 (vira `,`),
quebras de linha reais, `<think>`, cercas de código e caracteres fora do Windows-1252.
No EU4, bandeiras como `@FRA` e `@[Root.GetTag]` e escapes como `\"` e `\\` também são protegidos.
Pontos e vírgulas são permitidos nos valores EU4, e o conteúdo não passa pela conversão Windows-1252.

**4b. Trava de segurança (`safety`).** O tradutor só altera o texto dos `.csv` ou `.yml`; nunca toca nos scripts do mod.
Com `safety = "strict"` (padrão), antes de gravar cada linha ela é comparada com o original: se a quantidade de
`[ ] $ § £ { } \\` mudar, se os códigos do jogo forem diferentes, se os espaços das pontas mudarem ou houver
caracteres de controle, a linha **volta ao idioma original** e entra no relatório como `revertida`. Aspas e travessões
"tipográficos" que o original não usava viram ASCII. `safety = "paranoid"` também não traduz nenhuma linha que tenha
códigos do jogo. O `verify` aplica as mesmas regras. No EU4, a validação estrutural também é
obrigatória em `normal`, para preservar os códigos e escapes do YML.

**5. Cache, keep e glossário.** Cada tradução vai para o cache configurado, normalmente
`data/cache/cache.sqlite`; se interromper (Ctrl+C), é só rodar de novo. Textos repetidos são traduzidos
uma vez. Os termos fornecidos ficam em `data/terms/`
(mudar um termo refaz só as linhas que o contêm):

- `keep.txt`: nomes que **não** se traduzem, um por linha (`Winterfell`, `Night's Watch`).
- `glossary.txt`: traduções fixas, uma por linha: `Maester = Meistre`. Também aceita `->` ou `:`.
- Linhas vazias e as que começam com `#` são ignoradas. Arquivos em UTF-8. Sem `;` na tradução.
- Nomes diferentes ou vários arquivos: `keep_file` / `glossary_file` em `[translate]` (texto ou lista). `""` desliga.
- Os termos escritos direto em `[keep]` / `[glossary]` do `config.toml` continuam valendo e são somados aos arquivos.
- Casam sem diferenciar maiúsculas/minúsculas e só como palavra inteira (`King` não pega `Kingdom` nem `Kings`).
- Se o termo estiver capitalizado no original ("The King") e a tradução começar em minúscula, a inicial sai maiúscula.

Use, por exemplo, `keep_file = "data/terms/keep.txt"` e
`glossary_file = "data/terms/glossary.txt"` na configuração da raiz. Se esses caminhos não forem
configurados, os arquivos antigos ao lado da configuração têm prioridade; depois o programa procura
em `data/terms/`. Os exemplos indicam `cache = "data/cache/cache.sqlite"`; configurações que omitem
essa opção mantêm o padrão antigo `cache.sqlite` ao lado da configuração.

Como cada backend usa os termos: `ollama`/`openai` recebem no prompt ("manter exatamente" / "traduza X como Y").
`translators`, `argos` e `google` não entendem instruções, então o termo vira um marcador `{n}` (como os códigos do
jogo) e **a tradução do glossário é colocada no lugar depois** — é imposição exata, sem depender do serviço.
Cuidado: por isso um erro no glossário se repete em todas as linhas; revise antes de ativar.
Os arquivos fornecidos são voltados a Game of Thrones. O exemplo EU4 os desativa: crie seus próprios
arquivos ou use `[keep]` e `[glossary]` na configuração EU4.

Os dois perfis usam os mesmos backends, a mesma classe de cache SQLite e o mesmo processamento de
glossário e pós-edição. É possível apontar ambos para o mesmo banco, pois as chaves de tradução EU4
têm identificação do jogo. O formato das chaves CK2 foi preservado, mantendo o cache antigo válido.
Uma tradução CK2 não será aplicada automaticamente a um texto EU4 idêntico.

`python glossario_do_cache.py` lê `cache.sqlite` da raiz quando existe ou, caso contrário,
`data/cache/cache.sqlite`, e grava `saida/keep_sugerido.txt` e `saida/glossary_sugerido.txt`
(glossário todo comentado). Revise, ative o que estiver certo e incorpore a `data/terms/keep.txt` /
`data/terms/glossary.txt`. Também é possível informar um cache explicitamente:
`python glossario_do_cache.py data/cache/cache.sqlite --min 4`.

**6. Trocar de backend sem perder trabalho.** O cache guarda cada tradução com o nome do backend
(`ollama:<modelo>`, `google`, `argos`). Com `reuse_cache_from = "auto"` (padrão), o backend atual reaproveita o que
outro backend já traduziu **para o mesmo jogo** e só traduz o que falta, nos dois sentidos. Entradas antigas com defeito (marcadores
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
desligar com `ptbr_fix = false`. Você também pode listar correções em `data/terms/substituicoes.txt`
(`errado = certo`) e apontar `replace_file` para esse arquivo.
Isso roda SOBRE o texto com marcadores `{n}`, então códigos do jogo, `keep` e glossário nunca são tocados, e **não
entra no cache**: mudar as regras vale na hora, sem retraduzir nada.

## Uso

```
1. Escolha o backend no config.toml (backend = "translators", "argos", "google" ou "ollama") e instale o que ele pede
2. Copie examples/config.ck2.toml para config.toml na raiz (EU4: examples/config.eu4.toml para config.eu4.toml). Preencha [[mods]]
3. python ck2tr.py scan      --config config.toml            # analisa, sem IA e sem gravar nada (avisa arquivos repetidos entre mods)
4. python ck2tr.py status    --config config.toml            # quanto já está no cache, quanto falta e estimativa de tempo
5. python ck2tr.py translate --config config.toml --limit 30 # teste pequeno: leia o resultado!
6. python ck2tr.py translate --config config.toml            # traduz tudo (retomável)
7. python ck2tr.py verify    --config config.toml            # confere original x traduzido
8. python ck2tr.py package   --config config.toml            # verifica e gera saida/<mod>_ptbr.zip com LEIA-ME
9. Ou copie a pasta <mod>_ptbr/ e o arquivo <mod>_ptbr.mod para a pasta mod do jogo e ative DEPOIS do original
```

No EU4, você pode manter uma configuração separada `config.eu4.toml` e usar `--config config.eu4.toml`
em todos esses comandos, incluindo `scan`, `status`, `terms`, `translate`, `diff`, `verify` e `package`.
O exemplo começa em `backend = "echo"`, que só acrescenta `PT> ` para testar sem rede. Para obter
português, escolha um dos backends reais e use as mesmas instruções de instalação do CK2.
Instale o resultado em `Documentos/Paradox Interactive/Europa Universalis IV/mod` ou
`Documentos/Paradox Interactive/Crusader Kings II/mod`, conforme o perfil escolhido.

Comandos de apoio: `terms` (sugere nomes para o keep a partir dos textos), `diff` (original x traduzido; com
`--find sacrific` mostra as linhas que contêm o texto, sem `--find` só as que têm aviso), `argos-setup`.
`python glossario_do_cache.py` sugere `keep.txt` e `glossary.txt` a partir do cache já traduzido.
Outras opções: `--mod trecho` (só um mod), `--backend echo` (testa o pipeline sem IA), `--mode inplace` (somente CK2).
Caminhos relativos em `[[mods]]` valem a partir da pasta do `config.toml`. Chaves com erro de digitação em
`[translate]` geram um aviso. LM Studio e llama.cpp server funcionam com `backend = "openai"`.
Testes: `python -m unittest discover -v`, a partir da raiz do projeto.

O comando `status` lê o cache sem gravar arquivos nem carregar bibliotecas opcionais de tradução.
`diff` examina todas as colunas de destino configuradas. `verify` retorna erro quando não há arquivos
para conferir, e `package` recusa traduções com falhas estruturais. Os CSVs/YMLs, descritores, relatórios e
ZIPs são gravados por substituição atômica. Linhas sem as colunas de destino ficam intactas e entram
no relatório; o programa não acrescenta colunas ao CSV.

No EU4, `verify` também confere o BOM, a estrutura de cada entrada, os cabeçalhos e comentários
preservados e os códigos/escapes. `diff` compara os valores do idioma escolhido. O ZIP contém
os dois descritores e as instruções para instalar a tradução no jogo correspondente.

`check` é um alias de `verify` e aceita as mesmas opções. Os exemplos CK2/EU4 trazem os comandos
de tradução, check e scan. Em `overlay`, `translation_manifest.json` registra os arquivos gerados:
o check detecta arquivos apagados e o package recusa esse pacote incompleto. Uma tradução limitada
por `--limit` exige apenas os arquivos realmente gerados. Saídas antigas sem manifesto continuam
verificadas pelos arquivos presentes.

O glossário e o keep do EU4 também reconhecem termos imediatamente após códigos de cor, como
`§YEmpire§!`, preservando os códigos. A pós-edição é validada antes de restaurar os marcadores;
se uma regra alterar esses marcadores, a tradução válida anterior à regra é mantida.

**Se o jogo cair só com a tradução ativa:** rode `diff` (e `diff --find <trecho do evento>`), olhe `logs\error.log`
do jogo, e tente `safety = "paranoid"`. Para achar o arquivo culpado, tire arquivos de
`saida\<mod>_ptbr\localisation` aos poucos até parar de cair.

## Adicionar outros jogos

Somente CK2 e EU4 estão implementados. Os perfis ficam em `ck2translator/profiles.py`, incluindo
idiomas, contexto do prompt, extensão e política de descoberta dos arquivos. Para suportar outro
jogo, implemente seu parser, registre o formato em `ck2translator/localisation.py` e acrescente
as regras de configuração e de descritores necessárias. Os comandos usam essa camada comum,
enquanto os backends, cache, glossário e pós-edição continuam compartilhados.

Inclua testes de leitura/escrita sem perda, proteção de códigos, criação de mod separado e execução
dos comandos. Rode `python -m unittest discover -v` para conferir também a compatibilidade CK2.

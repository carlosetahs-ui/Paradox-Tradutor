# Tradutor de Mods Paradox — CK2 e EU4

Traduz a pasta `localisation` de mods do **Crusader Kings II** e do **Europa Universalis IV**
para português do Brasil, preservando chaves, códigos do jogo, comentários e quebras de linha.
Por padrão, cria um mod de tradução separado (`overlay`) e mantém o original intacto.
Configurações antigas continuam usando CK2, e a entrada `python ck2tr.py` continua compatível.

| Perfil | Arquivos | Gravação | Modos |
| --- | --- | --- | --- |
| `ck2` (padrão) | `localisation/*.csv` | Preserva encoding, colunas e marcador final `x` | `overlay` e `inplace` com backup |
| `eu4` | `localisation/**/*.yml` | UTF-8 com BOM; preserva cabeçalhos, versões e comentários | `overlay` obrigatório |

Requer **Python 3.11 ou superior**. O núcleo e os testes usam apenas a biblioteca padrão.

## Instalação

No PowerShell, a partir da pasta do projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[translators]"
```

Se a ativação estiver bloqueada, use `.\.venv\Scripts\python.exe` no lugar de `python`.
No Linux/macOS, ative com `source .venv/bin/activate`.

Instale somente o backend que vai usar:

| Backend | Instalação | Uso |
| --- | --- | --- |
| `translators` | `python -m pip install -e ".[translators]"` | Serviços online com troca de provedor e limites de pedidos |
| `google` | `python -m pip install -e ".[google]"` | Google via `deep-translator` |
| `argos` | `python -m pip install -e ".[argos]"` | Tradução offline; depois rode `python ck2tr.py argos-setup` |
| `ollama` | `python -m pip install -e .` | Modelo local; exige o servidor e o modelo instalados |
| `openai` | `python -m pip install -e .` | Servidor compatível, como LM Studio ou llama.cpp |
| `echo` | Nenhuma dependência | Teste do fluxo; acrescenta `PT> ` e não traduz |

Também é possível executar diretamente `python ck2tr.py` ou `python -m ck2translator`, sem instalar
o projeto. Nesse caso, instale a biblioteca do backend separadamente. Os serviços online recebem
os textos enviados para tradução.

## Configuração

Se ainda não tiver uma configuração local, copie [examples/config.ck2.toml](examples/config.ck2.toml)
para `config.toml` **na raiz do projeto**. Preserve sua configuração existente se já usa o tradutor.
Para EU4, copie [examples/config.eu4.toml](examples/config.eu4.toml) para `config.eu4.toml` na raiz.
Os exemplos contêm caminhos relativos preparados para essas cópias.

```toml
[game]
id = "ck2" # opcional: CK2 é o padrão quando esta seção não existe

[model]
backend = "translators"

[translate]
source_column = "english"
write_columns = ["english"]
mode = "overlay"
output_dir = "saida"
cache = "data/cache/cache.sqlite"
safety = "strict"
keep_file = "data/terms/keep.txt"
glossary_file = "data/terms/glossary.txt"
replace_file = "data/terms/substituicoes.txt"

[[mods]]
name = "A Game of Thrones"
path = 'mods/a game of thrones'
```

Extraia o mod antes de traduzir. `path` aponta para a pasta que contém `localisation`, e `name`
deve ser o nome exato do descritor `.mod` original para que a dependência funcione no jogo.
Todos os caminhos relativos, inclusive `mod_file`, são resolvidos a partir da pasta da configuração.
No Windows, use aspas simples nos caminhos com barras invertidas.

O CK2 não possui coluna de português. Com `write_columns = ["english"]`, selecione **inglês** no jogo.
Para ocupar todos os idiomas disponíveis, use `["english", "french", "german", "spanish"]`.

### Europa Universalis IV

```toml
[game]
id = "eu4"
# supported_version = "1.xx.*" # opcional; ajuste à versão do seu jogo

[model]
backend = "echo" # teste sem rede; troque pelo backend desejado para traduzir

[translate]
source_column = "english"
write_columns = ["english"]
mode = "overlay"
output_dir = "saida_eu4"
cache = "data/cache/cache.sqlite"
keep_file = ""
glossary_file = ""
replace_file = ""

[[mods]]
name = "Meu Mod EU4"
path = 'mods/meu_mod_eu4'
```

Os `.yml` do EU4 são lidos também em subpastas. A saída é sempre **UTF-8 com BOM**, mesmo que
o original não tenha BOM. Apenas o texto das entradas do idioma de origem é traduzido; chaves,
números de versão, espaços de formatação, comentários, escapes e códigos são preservados.
O cabeçalho nativo, por exemplo `l_english:`, e o nome original do arquivo são mantidos.
Linhas inválidas (por exemplo, sem aspas finais) são ignoradas na tradução e preservadas
sem alterações na saída. O programa avisa o arquivo, a linha e o motivo, e continua
traduzindo as entradas válidas. Isso é automático, sem opção adicional na configuração.
Use `source_column` e `write_columns` com o **mesmo idioma**; para PT-BR, o padrão é
`english` / `["english"]`, e você deve selecionar **inglês** no EU4. Não use `l_portuguese`.

EU4 exige `overlay`. Cada tradução gera a pasta `<mod>_ptbr`, um `<mod>_ptbr.mod` externo e um
`descriptor.mod` interno, com dependência pelo nome exato do mod original. Se `name` não estiver
informado, o programa tenta ler `descriptor.mod` do mod original. `supported_version` é opcional:
informe a versão compatível que você verificou. IDs de publicação, como `remote_file_id`, não são copiados.
Copie a pasta e o `.mod` externo para `Documentos/Paradox Interactive/Europa Universalis IV/mod`,
ative o original e a tradução no launcher e coloque a tradução depois do original.

O exemplo EU4 usa `echo` para testar sem chamadas reais.
Troque o backend usando as mesmas opções e instalações da tabela acima.
Os perfis compartilham a implementação dos backends, cache e glossário. O mesmo banco SQLite pode servir
aos dois jogos: as chaves do EU4 são separadas das do CK2, cujas entradas antigas continuam válidas.


## Tradução

```powershell
python ck2tr.py scan --config config.toml
python ck2tr.py status --config config.toml
python ck2tr.py terms --config config.toml
python ck2tr.py translate --config config.toml --limit 30
python ck2tr.py diff --config config.toml --find "trecho ou chave"
python ck2tr.py check --config config.toml
```

Depois de revisar o teste pequeno, traduza tudo e gere o pacote:

```powershell
python ck2tr.py translate --config config.toml
python ck2tr.py package --config config.toml
```

O ZIP inclui o mod de tradução, o descritor e as instruções para os idiomas escolhidos.
`package` executa a verificação estrutural e retorna erro se os arquivos não estiverem íntegros.
Teste a tradução dentro do jogo antes de distribuí-la; a verificação não avalia a qualidade do português.
`check` e `verify` executam a mesma verificação, sem chamar os serviços de tradução.
Novas saídas guardam `translation_manifest.json` com os arquivos gerados: se um deles for apagado,
a verificação acusa a ausência e o pacote é recusado. Traduções antigas sem esse arquivo continuam aceitas.

O cache permite retomar uma execução interrompida. O programa valida traduções armazenadas antes de usá-las.
Se uma linha não passar nas proteções, mantém o original e registra o motivo em
`saida/<mod>_relatorio.csv`. Uma nova execução atualiza esse relatório.
Reexecutar a tradução reconstrói os arquivos processados a partir do original; `--limit` limita os textos
incluídos nessa execução. Para a versão completa, execute novamente sem `--limit`.

| Comando/opção | Finalidade |
| --- | --- |
| `scan` | Analisa os arquivos do perfil escolhido e avisa sobre nomes em conflito |
| `status` | Consulta o cache sem carregar serviços nem gravar arquivos |
| `translate` | Traduz os mods selecionados |
| `check` / `verify` | Confere estrutura, códigos e conteúdo preservado; acusa ausência de arquivos |
| `diff` | Mostra avisos nos textos de destino, ou procura um trecho com `--find` |
| `package` | Verifica e empacota a tradução em modo `overlay` |
| `terms` | Sugere nomes para preservar |
| `--mod "trecho"` | Filtra mods pelo nome |
| `--backend echo` | Testa a tradução sem IA nem internet |
| `--limit N` | Limita a tradução aos primeiros N textos únicos de cada mod; N deve ser positivo |

Após instalar o projeto, `ck2tr` é equivalente a `python ck2tr.py`.
Para sugerir termos a partir do cache, use `python glossario_do_cache.py` ou `ck2-glossario`.
Sem argumentos, a ferramenta usa o cache antigo da raiz, quando existe, ou `data/cache/cache.sqlite`,
e grava as sugestões em `saida/`. Revise-as antes de incorporar aos arquivos de `data/terms/`.

Todos esses comandos funcionam com os dois perfis; use `--config config.eu4.toml` para sua configuração EU4.

No CK2, o modo `inplace` altera o mod original com backup em `saida/backup/` e bloqueia uma segunda execução
até que o original seja restaurado. Para o uso normal, mantenha `mode = "overlay"`.

## Organização

```text
ck2translator/          Código Python: comandos, formatos, perfis e backends
tests/                  Testes offline
docs/                   Documentação detalhada
examples/
    config.ck2.toml     Exemplo CK2 para copiar à raiz
    config.eu4.toml     Exemplo EU4 com teste offline
data/
    terms/              keep.txt, glossary.txt e substituicoes.txt versionados
    cache/              Cache SQLite local, ignorado pelo Git
mods/                   Mods originais locais, ignorados pelo Git
saida/                  Traduções CK2, relatórios, sugestões e pacotes gerados
saida_eu4/              Saída do exemplo EU4
config.toml             Configuração local ativa, ignorada pelo Git
config.eu4.toml         Configuração local EU4 opcional, ignorada pelo Git
ck2tr.py                Entrada de execução direta
glossario_do_cache.py   Entrada da ferramenta de termos
pyproject.toml          Instalação e dependências opcionais
```

As pastas de cache e saída são criadas quando necessárias. Os scripts e a configuração ativa
continuam na raiz para manter os comandos de execução. Os dados revisados ficam em `data/terms/`;
arquivos gerados, como `glossary_sugerido.txt`, ficam em `saida/`.

O [.gitignore](.gitignore) exclui a configuração local, ambientes virtuais, bancos de cache,
mods originais em `mods/`, saídas, backups, ZIPs e temporários. O exemplo e os termos revisados ficam
versionados. Se usar outras pastas de saída ou de mods dentro do projeto, acrescente-as ao ignore.

## Desenvolvimento

```powershell
python -m unittest discover -v
python -m unittest tests.test_improvements -v
python -m ck2translator --help
```

Os testes usam arquivos temporários e backends simulados, sem chamadas reais aos serviços.
A integração contínua está configurada para Windows e Linux, com Python 3.11 e 3.14.
No Windows, também verifica caminhos temporários abreviados (8.3), usados pelos runners do GitHub.
Os backends opcionais dependem da compatibilidade de suas próprias bibliotecas com a versão do Python.

Para adicionar um jogo, crie seu perfil em `profiles.py`, implemente o parser e registre o formato em
`localisation.py`, incluindo os descritores e os testes de preservação e integração necessários.
Somente CK2 e EU4 têm suporte implementado; escolher outro nome de jogo não adapta automaticamente seu formato.

Detalhes sobre proteção de códigos, glossário, revisão e limitações estão em
[Como o tradutor funciona](docs/funcionamento.md).

# CK2 Tradutor de Mods

Traduz a pasta `localisation` de mods do **Crusader Kings II** para português do Brasil,
preservando os códigos do jogo, as outras colunas, os comentários, o encoding e as quebras de linha.
Por padrão, cria um mod de tradução separado (`overlay`) e mantém o original intacto.

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

Se ainda não tiver uma configuração local, copie [config.example.toml](config.example.toml) para
`config.toml`. Preserve sua configuração existente se já usa o tradutor.

```toml
[model]
backend = "translators"

[translate]
source_column = "english"
write_columns = ["english"]
mode = "overlay"
output_dir = "saida"
cache = "cache.sqlite"
safety = "strict"

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

O [keep.txt](keep.txt) e o [glossary.txt](glossary.txt) fornecidos contêm termos de **Game of Thrones**.
Revise-os ao traduzir outros mods, ou desative-os com `keep_file = ""` e `glossary_file = ""`.
As correções de pós-edição ficam em [substituicoes.txt](substituicoes.txt).

## Tradução

```powershell
python ck2tr.py scan --config config.toml
python ck2tr.py status --config config.toml
python ck2tr.py translate --config config.toml --limit 30
python ck2tr.py diff --config config.toml --find "trecho ou chave"
python ck2tr.py verify --config config.toml
```

Depois de revisar o teste pequeno, traduza tudo e gere o pacote:

```powershell
python ck2tr.py translate --config config.toml
python ck2tr.py package --config config.toml
```

O ZIP inclui o mod de tradução, o descritor e as instruções para os idiomas escolhidos.
`package` executa a verificação estrutural e retorna erro se os arquivos não estiverem íntegros.
Teste a tradução dentro do jogo antes de distribuí-la; a verificação não avalia a qualidade do português.

O cache permite retomar uma execução interrompida. O programa valida traduções armazenadas antes de usá-las.
Se uma linha não passar nas proteções, mantém o original e registra o motivo em
`saida/<mod>_relatorio.csv`. Uma nova execução atualiza esse relatório.
Reexecutar a tradução reconstrói os arquivos processados a partir do original; `--limit` limita os textos
incluídos nessa execução. Para a versão completa, execute novamente sem `--limit`.

| Comando/opção | Finalidade |
| --- | --- |
| `scan` | Analisa os CSVs e avisa sobre nomes de arquivos em conflito |
| `status` | Consulta o cache sem carregar serviços nem gravar arquivos |
| `translate` | Traduz os mods selecionados |
| `verify` | Confere estrutura, códigos e colunas preservadas; acusa ausência de arquivos |
| `diff` | Mostra avisos nas colunas de destino, ou procura um trecho com `--find` |
| `package` | Verifica e empacota a tradução em modo `overlay` |
| `terms` | Sugere nomes para preservar |
| `--mod "trecho"` | Filtra mods pelo nome |
| `--backend echo` | Testa a tradução sem IA nem internet |
| `--limit N` | Limita a tradução aos primeiros N textos únicos de cada mod; N deve ser positivo |

Após instalar o projeto, `ck2tr` é equivalente a `python ck2tr.py`.
Para sugerir termos a partir do cache, use `python glossario_do_cache.py` ou `ck2-glossario`.

O modo `inplace` altera o mod original com backup em `saida/backup/` e bloqueia uma segunda execução
até que o original seja restaurado. Para o uso normal, mantenha `mode = "overlay"`.

## Organização

```text
ck2translator/          Código do aplicativo
    cli.py             Comandos e fluxo de tradução
    config.py          Validação de TOML e caminhos dos mods
    ck2loc.py          Leitura, escrita e verificação dos CSVs
    translator.py      Backends, proteção dos códigos e cache
    termos.py          Leitura dos arquivos de termos
    revisao.py         Pós-edição para português do Brasil
    glossario.py       Sugestões a partir do cache
    files.py           Gravação atômica de arquivos
tests/                 Testes offline
docs/                  Documentação detalhada
ck2tr.py               Entrada de execução direta
glossario_do_cache.py  Entrada da ferramenta de termos
config.example.toml    Modelo de configuração versionado
pyproject.toml         Instalação e dependências opcionais
```

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
Os backends opcionais dependem da compatibilidade de suas próprias bibliotecas com a versão do Python.

Detalhes sobre proteção de códigos, glossário, revisão e limitações estão em
[Como o tradutor funciona](docs/funcionamento.md).

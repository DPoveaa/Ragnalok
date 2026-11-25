# Guia Padronizado de Transporte com Coordenadas — Ragnarok Online LATAM

> **Padrão de Serviços Base:**
> * **Salvar Ponto de Retorno:** Gratuito em todas as Kafras e corporações equivalentes.
> * **Armazém:** **40z** por acesso (ou gratuito com *Ticket de Armazém*).
> * **Aluguel de Carrinho:** **800z** (apenas para classes da linha de Mercador / Superaprendiz com habilidade, nas Kafras habilitadas).

---

## 1. Rede Kafra Principal (Cidades com Teleporte)

Todas as Kafras abaixo oferecem **Salvar Ponto de Retorno**, **Armazém (40z)** e **Aluguel de Carrinho (800z)** — com exceção do **Farol de Pharos**, que não possui aluguel de carrinho.

| Cidade / Origem | Mapa e Coordenadas `(X, Y)` | Localização / NPC | Destinos de Teleporte e Preço |
| :--- | :--- | :--- | :--- |
| **Prontera** | `prontera (152, 191)` *(Centro)*<br>`prontera (146, 89)` *(Sul)*<br>`prontera (151, 288)` *(Norte)*<br>`prontera (282, 200)` *(Leste)*<br>`prontera (29, 207)` *(Oeste)* | Centro + 4 Portões (N, S, L, O) | • **Izlude:** 600z<br>• **Geffen:** 1.200z<br>• **Morroc:** 1.200z<br>• **Payon:** 1.200z<br>• **Caverna dos Orcs:** 1.700z<br>• **Alberta:** 1.800z |
| **Izlude** | `izlude (128, 148)` | Centro da cidade | • **Geffen:** 1.200z<br>• **Morroc:** 1.200z<br>• **Payon:** 1.200z<br>• **Al de Baran:** 1.800z |
| **Geffen** | `geffen (120, 62)` *(Centro)*<br>`geffen (203, 123)` *(Leste)* | Centro da cidade | • **Prontera:** 1.200z<br>• **Al de Baran:** 1.200z<br>• **Caverna dos Orcs:** 1.700z<br>• **Mina Abandonada (Mjolnir):** 1.700z |
| **Payon** | `payon (181, 104)` *(Centro)*<br>`payon (175, 226)` *(Norte)* | Centro da cidade | • **Prontera:** 1.200z<br>• **Alberta:** 1.200z<br>• **Morroc:** 1.200z |
| **Morroc** | `morocc (156, 97)` *(Centro)*<br>`morocc (160, 258)` *(Norte)* | Centro da cidade | • **Prontera:** 1.200z<br>• **Payon:** 1.200z<br>• **Farol de Pharos:** 1.200z<br>• **Alberta:** 1.800z<br>• **Comodo:** 1.800z |
| **Alberta** | `alberta (113, 60)` *(Centro)*<br>`alberta (28, 229)` *(Norte)* | Centro da cidade | • **Payon:** 1.200z<br>• **Morroc:** 1.800z<br>• **Prontera:** 1.800z |
| **Al de Baran** | `aldebaran (143, 119)` *(Centro)*<br>`aldeba_in (96, 181)` *(Interno QG)* | QG da Corporação Kafra | • **Geffen:** 1.200z<br>• **Juno:** 1.200z<br>• **Mina Abandonada (Mjolnir):** 1.700z<br>• **Izlude:** 1.800z |
| **Comodo** | `comodo (195, 150)` | QG Ocidental Kafra | • **Farol de Pharos:** 1.200z<br>• **Morroc:** 1.800z |
| **Farol de Pharos** | `cmd_fild07 (136, 134)` | Entrada do Farol *(sem carrinho)* | • **Comodo:** 1.200z<br>• **Morroc:** 1.200z |
| **Juno (Yuno)** | `yuno (152, 187)` *(Centro)*<br>`yuno (327, 108)` *(Leste)* | Centro da cidade | • **Al de Baran:** 1.200z<br>• **Einbroch:** 2.000z<br>• **Lighthalzen:** 2.000z<br>• **Hugel:** 2.000z<br>• **Rachel:** 2.000z |

> **Nota de Rota:** Prontera não teleporta diretamente para **Al de Baran** nem para **Comodo**. Use **Izlude** ou **Geffen** como escala para Al de Baran, e **Morroc** como escala para Comodo.

---

## 2. Sistema de Aeroplanos (Rotas e Teleportadores)

Existem duas formas de utilizar a rede aérea: embarcando fisicamente no **Aeroplano** (**1.200z**) ou falando com a **Equipe do Aeroplano** nas cidades para teleporte instantâneo (**1.800z**). Ambas aceitam **Ticket de Aeroplano Gratuito**.

### 2.1 Linha 1 — Aeroplano Internacional (`Izlude ↔ Juno ↔ Rachel`)

| Ponto de Embarque | Mapa e Coordenadas `(X, Y)` | NPC Responsável | Tarifa | Rota e Sequência de Paradas |
| :--- | :--- | :--- | :--- | :--- |
| **Izlude** *(Sudeste)* | `izlude (202, 75)` | Equipe do Aeroplano | **1.200z** | **Izlude** → **Juno** → **Rachel** |
| **Juno** *(Terminal Oeste — Esquerda)* | `yuno (59, 240)` *(Entrada)*<br>`y_airport (143, 43)` *(Saguão)* | Recepcionista do Aeroporto | **1.200z** *(Grátis na baldeação)* | **Juno** → **Izlude** ou **Rachel** |
| **Rachel** *(1 mapa a leste)* | `ra_fild12 (292, 208)` | Equipe do Aeroplano | **1.200z** | **Rachel** → **Juno** → **Izlude** |

### 2.2 Linha 2 — Aeroplano Doméstico (`Juno ↔ Hugel ↔ Einbroch ↔ Lighthalzen`)

| Ponto de Embarque | Mapa e Coordenadas `(X, Y)` | NPC Responsável | Tarifa | Rota e Sequência de Paradas |
| :--- | :--- | :--- | :--- | :--- |
| **Juno** *(Terminal Oeste — Direita)* | `yuno (59, 240)` *(Entrada)*<br>`y_airport (148, 43)` *(Saguão)* | Recepcionista do Aeroporto | **1.200z** *(Grátis na baldeação)* | **Juno** → **Hugel** → **Juno** → **Einbroch** → **Lighthalzen** |
| **Hugel** *(Leste)* | `hugel (178, 142)` | Equipe do Aeroplano | **1.200z** | **Hugel** → **Juno** → **Einbroch** → **Lighthalzen** |
| **Einbroch** *(Oeste)* | `einbroch (64, 207)` *(Entrada)*<br>`airport (143, 43)` *(Saguão)* | Recepcionista do Aeroporto | **1.200z** | **Einbroch** → **Lighthalzen** ou **Juno** → **Hugel** |
| **Lighthalzen** *(Sudeste)* | `lighthalzen (267, 75)` *(Entrada)*<br>`lhz_airport (143, 43)` *(Saguão)* | Recepcionista do Aeroporto | **1.200z** | **Lighthalzen** → **Einbroch** → **Juno** → **Hugel** |

### 2.3 Teleporte Instantâneo do Aeroplano (Sem Esperar Voo)

| Cidade de Origem | Mapa e Coordenadas `(X, Y)` | Tarifa | Destinos Disponíveis |
| :--- | :--- | :--- | :--- |
| **Prontera** | `prontera (116, 72)` | **1.800z** *(ou Ticket)* | Juno, Izlude, Einbroch, Lighthalzen, Hugel, Rachel |
| **Izlude** | `izlude (202, 75)` | **1.800z** *(ou Ticket)* | Juno, Izlude, Einbroch, Lighthalzen, Hugel, Rachel |
| **Juno** | `yuno (59, 240)` | **1.800z** *(ou Ticket)* | Juno, Izlude, Einbroch, Lighthalzen, Hugel, Rachel |
| **Einbroch** | `einbroch (64, 207)` | **1.800z** *(ou Ticket)* | Juno, Izlude, Einbroch, Lighthalzen, Hugel, Rachel |
| **Lighthalzen** | `lighthalzen (267, 75)` | **1.800z** *(ou Ticket)* | Juno, Izlude, Einbroch, Lighthalzen, Hugel, Rachel |
| **Hugel** | `hugel (178, 142)` | **1.800z** *(ou Ticket)* | Juno, Izlude, Einbroch, Lighthalzen, Hugel, Rachel |
| **Rachel** | `ra_fild12 (292, 208)` | **1.800z** *(ou Ticket)* | Juno, Izlude, Einbroch, Lighthalzen, Hugel, Rachel |

> **Baldeação em Juno:** Ao desembarcar em Juno, desça as escadas até o saguão principal e fale com a recepcionista do lado oposto para trocar entre a Linha Internacional e a Linha Doméstica **sem pagar nova tarifa**.

---

## 3. Cidades e Expansões Sem Teleporte (Apenas Apoio Local)

Oferecem exclusivamente **Salvar Ponto de Retorno** e **Armazém (40z)** (além de carrinho apenas em Einbroch, Lighthalzen e Rachel).

| Cidade / Localidade | Mapa e Coordenadas `(X, Y)` | NPC / Corporação | Serviços Disponíveis |
| :--- | :--- | :--- | :--- |
| **Einbroch** | `einbroch (242, 205)` | Eventos Incríveis / Kafra | Salvar, Armazém, Carrinho |
| **Lighthalzen** | `lighthalzen (164, 100)` *(Centro)*<br>`lighthalzen (191, 320)` *(Norte)* | Eventos Incríveis / Kafra | Salvar, Armazém, Carrinho |
| **Einbech** | `einbech (181, 132)` | Eventos Incríveis / Kafra | Salvar, Armazém |
| **Hugel** | `hugel (88, 168)` | Eventos Incríveis / Kafra | Salvar, Armazém |
| **Veins** | `veins (208, 128)` | Eventos Incríveis / Kafra | Salvar, Armazém |
| **Rachel** | `rachel (109, 138)` | Eventos Incríveis / Kafra | Salvar, Armazém, Carrinho |
| **Lutie** | `xmas (148, 134)` | Funcionária Kafra | Salvar, Armazém |
| **Umbala** | `umbala (87, 160)` | Funcionária Kafra | Salvar, Armazém |
| **Nifflheim** | `niflheim (202, 180)` | Funcionária Kafra | Salvar, Armazém |
| **Jawaii** | `jawaii (213, 217)` | Funcionária Kafra | Salvar, Armazém |
| **Amatsu** | `amatsu (102, 149)` | Funcionária Kafra | Salvar, Armazém |
| **Kunlun (Gonryun)** | `gonryun (159, 122)` | Funcionária Kafra | Salvar, Armazém |
| **Louyang** | `louyang (210, 104)` | Funcionária Kafra | Salvar, Armazém |
| **Ayothaya** | `ayothaya (212, 169)` | Funcionária Kafra | Salvar, Armazém |
| **Moscovia** | `moscovia (223, 191)` | Funcionária Kafra | Salvar, Armazém |
| **Brasilis** | `brasilis (197, 221)` | Funcionária Kafra | Salvar, Armazém |
| **Dewata** | `dewata (202, 184)` | Funcionária Kafra | Salvar, Armazém |
| **Port Malaya** | `malaya (71, 79)` *(Porto)*<br>`malaya (234, 204)` *(Centro)* | Funcionária Kafra | Salvar, Armazém |
| **Acampamento da Expedição** | `mid_camp (216, 284)` | Pata de Gato / Eventos Incríveis | Salvar, Armazém |
| **Manuk** | `manuk (262, 177)` | Funcionária Kafra / Gato | Salvar, Armazém |
| **Splendide** | `splendide (194, 162)` | Funcionária Kafra / Gato | Salvar, Armazém |
| **El Dicastes** | `dicastes01 (198, 187)` | Funcionária Kafra | Salvar, Armazém |
| **Mora** | `mora (48, 128)` | Funcionária Kafra | Salvar, Armazém |
| **Eclage** | `eclage (112, 39)` *(Entrada)*<br>`eclage (295, 307)` *(Praça)* | Funcionária Kafra | Salvar, Armazém |
| **Rock Ridge** | `harboro1 (291, 207)` | Funcionária Kafra | Salvar, Armazém |
| **Verus** | `verus04 (123, 250)` | Eventos Incríveis | Salvar, Armazém |
| **Issgard** | `jor_tail (182, 204)`<br>`icas_in (180, 62)` | Eventos Incríveis | Salvar, Armazém |
| **Malangdo** | `malangdo (147, 117)`<br>`malangdo (184, 138)` | Gato da Pata de Gato | Salvar, Armazém |
| **Lasagna** | `lasagna (298, 238)` | Gato da Pata de Gato | Salvar, Armazém |

---

## 4. Kafras de Calabouços e Campos (Apenas Apoio)

Oferecem exclusivamente **Salvar Ponto de Retorno** e **Armazém (40z)**.

| Local de Apoio | Mapa e Coordenadas `(X, Y)` | Serviços Disponíveis | Acesso Principal |
| :--- | :--- | :--- | :--- |
| **Vila dos Arqueiros** | `pay_arche (55, 123)` | Salvar, Armazém | Portal norte de Payon |
| **Ilha Byalan (Túnel Submarino)** | `izlu2dun (106, 58)` | Salvar, Armazém | Barco no porto de Izlude (150z) |
| **Caverna dos Orcs** | `gef_fild10 (73, 340)` *(também `gef_f10_a/b/c`)* | Salvar, Armazém | Teleporte Kafra de Prontera ou Geffen (1.700z) |
| **Mina Abandonada de Mjolnir** | `mjolnir_02 (82, 362)` | Salvar, Armazém | Teleporte Kafra de Geffen ou Al de Baran (1.700z) |
| **Entrada de Glast Heim** | `glast_01 (200, 273)` | Salvar, Armazém | 2 mapas a oeste de Geffen |
| **Pirâmides** | `moc_ruins (59, 157)` | Salvar, Armazém | Noroeste de Morroc |
| **Esfinge** | `moc_fild19 (107, 100)` | Salvar, Armazém | Oeste de Morroc |
| **Fábrica de Brinquedos** | `xmas (148, 134)` *(Centro)*<br>`xmas (140, 297)` *(Portal Norte)* | Salvar, Armazém | Portal norte de Lutie |

---

## 5. Portos, Trens e Atalhos Especiais (Rotas Complementares)

| Origem | Mapa e Coordenadas `(X, Y)` | NPC / Meio de Transporte | Destino | Tarifa |
| :--- | :--- | :--- | :--- | :--- |
| **Izlude** *(Porto Sul)* | `izlude (197, 78)` | Marinheiro | **Alberta** / **Jawaii** *(100.000z)* | **500z** / **100.000z** |
| **Izlude** *(Porto Norte)* | `izlude (182, 186)` | Marinheiro | **Ilha Byalan (`izlu2dun`)** | **150z** |
| **Alberta** *(Porto)* | `alberta (189, 151)` | Marinheiro | **Ilha Byalan** / **Navio Fantasma (`alb2trea`)** | **500z** / **250z** |
| **Alberta** *(Docas Leste)* | `alberta (245, 93)` *(região `245–247, 42–115`)* | Capitães / Agentes de Turismo | **Amatsu**, **Kunlun**, **Louyang**, **Ayothaya**, **Moscovia**, **Brasilis**, **Dewata**, **Port Malaya** | **10.000z** *(ida)* / **Grátis** *(volta)* |
| **Al de Baran** *(Centro)* | `aldebaran (168, 168)` | Papai Noel | **Lutie (`xmas`)** *(volta em `xmas (143, 312)`)* | **Grátis** |
| **Einbroch ↔ Einbech** | `einbroch (232, 272)` ↔ `einbech (39, 215)` | Estação de Trem | **Einbech** ↔ **Einbroch** | **200z** |
| **Cidades Principais** | `prontera (156, 191)` *(e ao lado das Kafras centrais)* | Oficial do Grupo do Éden | **QG do Grupo do Éden (`moc_para01`)** *(saída em `30, 10`)* | **Grátis** |
| **Mapa-Múndi (`Ctrl + '`)** | Qualquer mapa elegível | Passaporte / Fruto de Miau-Miau | Cidades e arredores de calabouços | **Consumível** |
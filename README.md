# ForgeOS

**Commerce and market intelligence for physical product businesses.**

ForgeOS helps a business evaluate a physical product before spending money on production. From one plain-language requirement, it identifies the required component categories, checks current listings, creates procurement plans, tests supply disruptions, estimates material gross profit and produces a readiness decision.

Instead of checking products, suppliers, costs, risks and selling prices separately, ForgeOS brings the decision into one place.

## Features

- Converts an idea into the functional components required for a working product.
- Uses current product, merchant, price, pack-quantity and delivery evidence.
- Creates distinct procurement strategies within the requested quantity and budget.
- Tests supplier outages, price increases, budget cuts and shorter deadlines.
- Simulates price, availability and delivery risks.
- Compares the proposed selling price with similar finished products.
- Calculates revenue and material gross profit, not net profit.
- Finds nearby fabrication and prototyping providers.
- Explains what passed and what still needs confirmation before purchasing.

ForgeOS does not place orders, guarantee sales or replace engineering review.

## Architecture

```text
Browser: index.html
       |
Python API: server.py
       |
ForgeOS engine: forgeos.py
   |         |          |            |
 Ollama   SerpApi    OR-Tools      SQLite
 Qwen3    Shopping    CP-SAT       cache/store
          + Maps
       |
NetworkX dependency analysis + NumPy risk simulation
```

- `index.html` contains the complete HTML, CSS and JavaScript frontend.
- `server.py` serves the application and JSON API. Credentials never enter the browser.
- `forgeos.py` contains validation, market adapters, optimization, simulations, scenarios and readiness logic.
- `test_forgeos.py` contains deterministic tests using synthetic fixtures.
- `requirements.txt` pins the Python dependencies.
- `.env.example` documents configuration without exposing secrets.

### Responsibility boundaries

- **Qwen3:4b:** proposes the functional product structure. A deterministic recovery plan handles temporary local-model failures.
- **SerpApi:** supplies current product, merchant, price, delivery and local-business evidence.
- **Deterministic code:** controls quantities, pack calculations, budgets, optimization, simulations, financial arithmetic and final readiness.
- **User:** confirms uncertain specifications and approves purchases.

## SerpApi usage

SerpApi directly affects the result:

1. `google_shopping` provides evidence for each required component category.
2. A separate Shopping search provides comparable finished-product prices.
3. `google_maps` identifies nearby fabrication and prototyping providers.
4. This evidence feeds procurement optimization, supplier-risk checks, commercial analysis and readiness decisions.

Responses are cached in SQLite. Monthly and per-operation limits protect the search allowance. The API key is never sent to the browser or committed to Git.

## Technology

Python, Ollama `qwen3:4b`, SerpApi, Pydantic, OR-Tools CP-SAT, NetworkX, NumPy, SQLite, and vanilla HTML/CSS/JavaScript.

## Setup

```bash
git clone https://github.com/may-vaz/gap-radar.git
cd gap-radar
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
ollama pull qwen3:4b
cp .env.example .env
```

On Windows, activate with `venv\Scripts\activate`.

Add your key to `.env`:

```dotenv
SERPAPI_API_KEY=your_key_here
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:4b
PORT=4280
```

Keep the remaining search-limit and cache settings from `.env.example`. Never commit `.env`.

Run Ollama, then start ForgeOS:

```bash
python3 server.py
```

Open `http://localhost:4280`.

## Tests

```bash
python3 -m py_compile forgeos.py server.py test_forgeos.py
python3 -m unittest -v test_forgeos.py
```

Tests cover budget and quantity feasibility, supplier exclusion, search limits, reproducible risk simulation, commercial calculations, Qwen recovery and What-if re-optimization.

## API

```text
GET  /api/health
GET  /api/projects
GET  /api/projects/{id}
POST /api/compile
POST /api/projects/{id}/heal
POST /api/projects/{id}/scenario
```

## Limitations

- Listings may omit technical specifications; uncertain products require manual confirmation.
- Prices and availability should be refreshed before purchasing.
- Comparable-product prices are directional evidence, not proof of demand.
- Fabrication results indicate possible capability; businesses must request quotations.
- Risk results support decisions but do not guarantee supplier performance.

## AI disclosure

ForgeOS uses local Ollama `qwen3:4b` for functional design proposals. Deterministic code controls market calculations, optimization, simulations and readiness. Arena.ai Agent Mode assisted with implementation, debugging, testing and documentation.

## Demo

[Watch the ForgeOS demonstration video](https://drive.google.com/drive/folders/1sAnJQkVSjyTXtRRIPt26ZeRJG6ATMghe?usp=sharing)



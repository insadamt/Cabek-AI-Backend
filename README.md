# CABEK.AI — Application globale

## API FastAPI

Les endpoints de lecture des dossiers, résultats enregistrés, images et PDF sont
documentés dans [docs/api.md](docs/api.md). Le contrat des fichiers produits par
le moteur est décrit dans [docs/analysis-artifacts.md](docs/analysis-artifacts.md).
Le format de `metadata.json` est décrit dans [docs/metadata.md](docs/metadata.md) :
les identifiants `marque_id` et `model_id` sont des entiers positifs fournis
ensemble, et `marque` et `modele` contiennent les noms enregistrés. Les anciens
dossiers contenant uniquement les noms restent compatibles.
Le scan utilise les modèles existants dans un processus séparé, avec suivi des
fichiers générés et sauvegarde du résultat et du rapport PDF.

```bash
pip install -r requirements-api.txt
pip install -r requirements-scan.txt
python -m api --reload
```

Un port local libre est choisi automatiquement. Utilisez l'URL affichée dans le
terminal, puis ajoutez `/docs` pour ouvrir la documentation interactive.

L'application est maintenant une seule application Streamlit avec une navigation globale dans la barre latérale.

## Lancement

```powershell
pip install -r requirements.txt
python -m streamlit run app.py
```

## Navigation

- Accueil
- Collecte des données
- Expertise IA

Les anciennes interfaces séparées ne sont plus nécessaires pour lancer le système. Les pages dans `pages/` sont des pages de la même application Streamlit.

## Structure

```text
CABEK_AI_GLOBAL/
├── app.py
├── pages/
│   ├── 1_Accueil.py
│   ├── 2_Collecte.py
│   └── 3_Expertise_IA.py
├── core/
│   ├── dossier_manager.py
│   ├── expertise_engine.py
│   └── ui.py
├── bareme_c2.py
├── models/
│   ├── best_pieces.pt
│   ├── best.pt
│   └── best_zone_critique.pt
└── data/dossiers/
```

Le modèle `best_zone_critique.pt` est utilisé par le moteur pour intersecter le masque de zone critique avec le masque d'une `d_bosse`. Si l'intersection dépasse le seuil configuré, la bosse est forcée à `high` et le remplacement est déclenché par les règles métier existantes.

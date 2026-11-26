# Villa Zen'O — vérification iOS Safari

Ce dépôt teste les pages du portail invité dans le **vrai Safari d'iOS**, sur des iPhone simulés par Apple (Simulateur Xcode), grâce aux Mac gratuits de GitHub Actions.

À chaque envoi sur `main` (ou en lançant le workflow à la main) :

1. un iPhone 15 (ou le modèle le plus proche disponible) et un iPhone SE démarrent, clavier matériel débranché ;
2. Safari ouvre chaque page ;
3. le test touche réellement les boutons + / −, le champ Allergies, tape au vrai clavier iOS, fait défiler, puis sort du champ ;
4. il vérifie : page sur un seul écran, aucun zoom, champ visible au-dessus du clavier, champ et bouton sans chevauchement, barre du bas revenue exactement à sa place ;
5. captures, vidéo et `results.json` sont publiés sur les branches `results-big` et `results-small`.

Aucune donnée réelle d'invité, aucun lien invité, aucune clé ni aucun mot de passe ne doivent être ajoutés ici : le dépôt est public.

Les maquettes testées sont dans `site/`. Pour tester une autre adresse (un aperçu Vercel public, par exemple), lancer le workflow à la main avec le champ « Adresse à tester ».

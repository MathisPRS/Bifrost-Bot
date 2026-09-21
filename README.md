# Bifrost

Bot Discord qui allume et éteint un hyperviseur Proxmox et les serveurs de jeu qu'il
héberge — jusqu'à couper physiquement le courant, mais seulement une fois l'extinction
**prouvée**.

Il tourne sur une machine tierce toujours allumée (un NAS) et pilote une chaîne à quatre
étages :

```
prise connectée  →  hôte Proxmox  →  VM  →  conteneur du jeu
```

Pour éteindre, il faut redescendre les quatre dans l'ordre. Pour allumer, les remonter.
À chaque étage, on ne passe au suivant qu'une fois le précédent vérifié.

## Pourquoi ce n'est pas juste un script

Le SSD de la machine cible est sans protection contre les coupures. Couper le courant
d'une machine qui tourne encore, c'est risquer le thin-pool LVM et les disques de VM. Or
**une machine qui ne répond plus au réseau n'est pas forcément éteinte** : carte réseau
tombée, noyau planté, arrêt bloqué. Le réseau ne suffit donc pas à décider.

D'où la mesure de consommation de la prise : c'est une preuve *physique*, indépendante du
réseau. Et d'où toute l'architecture du projet, qui tient dans une seule primitive.

## La porte de vérification

Chaque étape est une **porte** : une condition, un délai de maintien, un timeout. Trois
règles, et chacune vient d'un incident réel :

**Une condition doit *tenir*, pas seulement apparaître.** Un cycle de redémarrage produit
un creux de consommation de quelques secondes, indiscernable d'une extinction sur une
mesure unique. On exige donc que la condition reste vraie pendant une durée.

**Une lecture ratée n'est jamais une valeur.** Timeout, erreur réseau, champ absent →
résultat `UNKNOWN`, qui remet le compteur de maintien à zéro exactement comme une valeur
hors critère. Confondre « je n'ai pas pu lire » et « zéro watt », c'est couper le courant
d'une machine allumée parce que le Wi-Fi de la prise a hoqueté.

**Une porte qui tombe arrête la séquence.** Rien n'est forcé, rien n'est coupé, et le
rapport dit exactement où on s'est arrêté. Aucun chemin du code ne demande un arrêt brutal
à l'hyperviseur. Le pire scénario est une machine restée allumée, jamais une machine
coupée à chaud.

## Aucun agent à déployer

Un agent installé sur la machine cible ne peut pas témoigner de sa propre mort : les
étapes qui comptent (« la VM est arrêtée », « l'hôte ne répond plus », « la consommation
est tombée ») sont par construction *hors* de la machine. Le bot est donc seul, et parle
quatre protocoles déjà présents :

| Besoin | Canal | Reste vivant quand |
|---|---|---|
| joueurs, logs, arrêt du conteneur, extraction du monde | API Docker sur transport SSH | la VM tourne |
| compte de joueurs, signal rapide | requête A2S UDP | le jeu tourne |
| état / arrêt / démarrage VM et hôte | API Proxmox (token restreint) | l'hôte tourne |
| la machine est-elle réellement éteinte | puissance de la prise | toujours |

Ces canaux sont **emboîtés** : à chaque étage, il reste un observateur au-dessus pour
constater ce qui s'est passé en dessous.

La clé SSH est verrouillée dans `authorized_keys` sur la seule commande
`docker system dial-stdio` : elle ne peut ni ouvrir de shell, ni rebondir ailleurs. Même
la copie du monde passe par `get_archive()` de l'API Docker — ni rsync, ni shell, ni
seconde clé. Le token Proxmox porte un rôle limité à `VM.Audit`, `VM.PowerMgmt`,
`Sys.Audit`, `Sys.PowerMgmt` : il peut éteindre et démarrer, pas détruire une VM.

## Commandes

| Commande | Effet | Droits |
|---|---|---|
| `/status [jeu]` | état complet, ne modifie rien | ouvert à tous |
| `/start [jeu]` | prise → hôte → VM → Docker → conteneur → jeu joignable | rôle du jeu |
| `/stop [jeu]` | 0 joueur → arrêt conteneur → copie du monde → VM → hôte → preuve → coupure | rôle du jeu |
| `/save [jeu]` | copie du monde, sans rien arrêter | rôle du jeu |
| `/restart [jeu]` | copie d'assurance → arrêt → copie à froid → redémarrage | rôle du jeu |
| `/allowrole [jeu] [rôle]` | change le rôle autorisé pour un jeu | rôle administrateur |

Le message Discord **s'édite en direct** : tout le trajet s'affiche dès la première
seconde, la ligne en cours se met à jour pendant les attentes (les watts qui montent, un
décompte `tenu 18/30 s`), et les étapes à venir restent visibles — quand ça s'arrête, on
voit d'un coup d'œil tout ce qui n'a *pas* été exécuté.

Les séquences destructives passent par un compte à rebours annulable, avec attribution
nominative de l'annulation.

Le rôle habilité à changer les rôles vit dans `secrets.env`, **hors base**, à dessein :
s'il était modifiable par `/allowrole`, un seul appel malheureux verrouillerait tout le
monde dehors.

## Plusieurs jeux

`core/` ne contient pas une seule fois le nom d'un jeu. Il ne connaît qu'un protocole :

```python
class GameDriver(Protocol):
    def is_up(self) -> bool: ...
    def presence(self) -> Presence: ...
    def last_save(self) -> SaveInfo: ...
    def snapshot(self, dest: Path, min_generation: int | None) -> BackupReport: ...
    def stop(self, timeout_s: int): ...
    def start(self): ...
    def parse_log_line(self, line: str, at: datetime) -> Event | None: ...
```

Ajouter un jeu = un fichier dans `games/`, une ligne dans `games/registry.py`, une entrée
dans `config.yaml`. Un jeu déclaré sans pilote doit rester `enabled: false` — le bot
refuse de démarrer plutôt que de faire semblant de savoir le sauvegarder.

Comme `/stop` coupe la **machine** et pas un jeu, une porte vérifie qu'aucun autre jeu
déclaré ne tourne avant d'éteindre quoi que ce soit.

## Sauvegardes

**On ne sauvegarde jamais un serveur vivant.** L'ordre dissout la course plutôt que de
tenter de la détecter : vérifier que personne n'est connecté, arrêter le conteneur, *puis*
copier. Une fois le conteneur mort, le port de jeu est fermé : plus personne ne peut se
connecter, plus rien n'écrit.

La copie est vérifiée sur les octets **effectivement posés à destination**, pas sur la
source, et un dossier ne prend son nom définitif qu'une fois validé. Pour Valheim, les
contrôles s'appuient sur le témoin d'intégrité que le jeu écrit en dernier et sur son
compteur de génération, recoupé avec le journal du serveur — deux preuves indépendantes du
même évènement.

## Mise en route

```bash
cp secrets.env.example secrets.env && chmod 600 secrets.env   # puis remplir
cp config.example.yaml config.yaml                            # puis adapter
cp docker-compose.example.yml docker-compose.yml              # puis adapter les chemins
docker compose build && docker compose up -d
```

Une CLI en lecture seule permet de tout vérifier avant de brancher Discord :

```bash
python -m bifrost.cli status          # état complet, ne modifie rien
python -m bifrost.cli power --watch 120   # courbe de consommation, pour calibrer
python -m bifrost.cli discord-probe   # liste les ID Discord visibles par le bot
```

### Calibrer les seuils — ne pas les deviner

`allow_cut: false` par défaut : la séquence d'extinction va jusqu'au bout mais s'arrête
**avant** de couper la prise. Chaque relevé de puissance est enregistré en base, ce qui
permet de mesurer la courbe réelle avant d'activer la coupure.

Deux erreurs faites sur ce projet, et qu'un seuil deviné reproduira :

- un plancher traitant `0 W` comme « capteur muet » — la prise utilisée renvoie
  *exactement* zéro sous son seuil de résolution, et la porte d'extinction n'a jamais pu
  passer ;
- un seuil « allumé » calibré sur une machine qui **faisait tourner le jeu** : au repos
  elle consommait deux fois moins, et le démarrage échouait.

La preuve d'extinction est donc une **chute observée** — consommation relevée au début de
la séquence, comparée à la fin — plutôt qu'une valeur absolue. Une prise bloquée sur zéro
n'aurait jamais passé le relevé initial.

## Une asymétrie volontaire

Pour **couper** le courant, on exige que tout concorde : réseau muet pendant une durée
soutenue, *et* chute de consommation maintenue, *et* un délai d'attente pour les caches
disque. Pour constater qu'une machine est **allumée**, une seule preuve positive suffit :
la consommation ou l'hôte qui répond.

C'est délibéré. Se tromper en déclarant allumée une machine allumée ne coûte rien. Se
tromper dans l'autre sens coûte un système de fichiers.

## Licence

MIT.

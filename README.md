# TableTop Tracker

Registro de partidas de board games con ranking Elo multijugador y recomendador
de qué jugar según quién está disponible y cuánto tiempo tenéis.

## Stack
- Flask + SQLite
- Ollama (llama3) para la frase de recomendación
- Vanilla HTML/CSS/JS, Chart.js para la evolución de Elo
- Estética neón/gaming: fondo oscuro con luces ambiente en violeta/cian/magenta,
  tipografía Archivo Black con efecto de sombra apilada para el branding.

## Cómo arrancarlo

```bash
pip install -r requirements.txt --break-system-packages
python app.py
```

La primera vez que arranca sin `instance/tabletop.db`, se inicializa la base
de datos automáticamente (usuarios, juegos, partidas).

Abre http://localhost:5000, regístrate y ya puedes:

1. Añadir juegos a la ludoteca (`/games/new`)
2. Registrar partidas con puestos y puntuaciones (`/matches/new`) → actualiza
   el Elo de cada jugador automáticamente
3. Ver el ranking del grupo (`/ranking`)
4. Ver la evolución de Elo de cada jugador (`/players/<id>`)
5. Pedir una recomendación según quién juega y el tiempo disponible (`/recommend`)
   — necesita Ollama corriendo en local (`ollama run llama3`) para la frase con IA;
   si no está disponible, simplemente no muestra el comentario extra.

## Elo multijugador

En vez de Elo 1v1, cada partida compara a cada jugador contra todos los demás
por pares según su puesto final, y promedia el cambio. K=32.

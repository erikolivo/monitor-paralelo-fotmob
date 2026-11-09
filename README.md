# monitor-paralelo-fotmob

Corre EN PARALELO al sistema de producción (Mimoexcel) sin tocarlo. No lee ni
escribe nada del repo real — es puramente de validación/comparación entre los
datos en vivo de ESPN y las estadísticas (xG, posesión, tiros) de FotMob.

## Qué hace

1. Consulta a ESPN los partidos en vivo del día.
2. Para cada uno, busca su `matchId` en FotMob vía el endpoint público de
   búsqueda (`apigw.fotmob.com/searchapi/suggest`), usando los nombres de los
   equipos — sin necesitar ningún slug guardado de antemano.
3. Con ese `matchId`, pide la página del partido en FotMob y extrae el bloque
   `__NEXT_DATA__` embebido en el HTML.
4. Imprime lado a lado: goles/minuto de ESPN vs xG de FotMob.
5. Guarda cada resultado como una línea JSON en `log_comparacion_fotmob.jsonl`
   para revisión posterior.

## Limitación conocida

FotMob solo incluye `content` (con las estadísticas) en el HTML para
partidos pre-renderizados (populares/recientes). Para partidos menos
populares, el script lo reporta explícitamente como
`sin_datos_prerenderizados` — nunca inventa datos.

## Uso local

```bash
pip install -r requirements.txt
python monitor_paralelo_fotmob.py        # un solo ciclo
python monitor_paralelo_fotmob.py --loop # loop local cada 5 min
```

## GitHub Actions

El workflow `.github/workflows/monitor.yml` corre un ciclo cada 10 minutos
(y también se puede lanzar a mano con `workflow_dispatch`). Al terminar,
si el log cambió, lo commitea de vuelta al repositorio con el mensaje
`Actualiza log de comparacion [skip ci]`.

> Importante: los workflows programados solo se ejecutan en la rama por
> defecto del repositorio. Asegúrate de que la rama por defecto sea la que
> contiene `.github/workflows/monitor.yml` (por ejemplo `main`).


# WSC 2026 Simulation Challenge - Round 1

Este proyecto contiene el codigo Python para la ronda 1 del WSC 2026 Simulation Challenge. Simula una red maritima de contenedores, aplica disrupciones planificadas y genera archivos CSV para analizar el desempeno de la red en un dashboard local.

## Regla Importante De La Competencia

Las instrucciones del reto indican que cualquier modificacion de solucion debe colocarse dentro de:

```text
response_strategies/
```

Esto incluye algoritmos nuevos y documentos explicativos. Los cambios hechos en otras carpetas pueden servir para analisis local, pero no seran considerados como parte de la entrega competitiva.

## Que Hay En El Proyecto

```text
.
+-- main.py
+-- requirements.txt
+-- simulation_output_csv_writer.py
+-- config/
+-- dashboard/
+-- Input/
+-- maritime_data_context/
+-- o2despy/
+-- Output/
+-- response_strategies/
+-- scenario_builders/
+-- simulation_model/
```

### Archivos Y Carpetas Principales

- `main.py`: punto de entrada. Construye el escenario, corre warm-up, ejecuta la simulacion medida, escribe CSVs y lanza el dashboard local.
- `config/`: constantes de configuracion como dias de warm-up, dias de simulacion, intervalo estadistico y activacion de estrategias.
- `Input/`: datos base de puertos, rutas, segmentos, demanda, clases de buques y plan de flota.
- `scenario_builders/`: crea el escenario base y el escenario con disrupciones.
- `simulation_model/`: motor de simulacion de eventos discretos. Contiene generadores, actividades, colas, navegacion, carga/descarga y manejo de disrupciones.
- `maritime_data_context/`: entidades del dominio maritimo: `Port`, `Vessel`, `Shipment`, `Booking`, `ServiceRoute`, `Leg`, `Demand`, etc.
- `response_strategies/`: zona de trabajo para la solucion del concursante.
- `dashboard/`: dashboard HTML/JS/CSS que consume los CSVs de `Output/`.
- `Output/`: salida generada por la simulacion. El dashboard espera varios CSVs aqui.
- `o2despy/`: libreria local O2DES usada por el motor de simulacion.

## Que Hace La Simulacion

La simulacion modela el flujo de contenedores desde origen hasta destino a traves de una red de servicios maritimos ciclicos.

El flujo general es:

1. Se generan shipments segun la matriz de demanda.
2. Cada shipment recibe una cadena de bookings.
3. Los shipments esperan en puerto de origen o transbordo.
4. Los buques navegan por sus rutas asignadas.
5. Al llegar a puerto, los buques hacen cola para atraque.
6. El puerto asigna berth disponible.
7. El buque carga y descarga contenedores.
8. Los shipments completados salen del sistema.
9. Se registran metricas de espera, transito, utilizacion y tiempo promedio de transporte.

La metrica principal de interes es el `AverageTransportTime`, escrito en:

```text
Output/ATT_By_Statistics_Interval.csv
```

## Escenario Actual

Por defecto `main.py` usa el escenario con disrupciones:

```python
context = scenario_builders.create_with_disruption()
```

El escenario base sin disrupciones tambien existe, pero esta comentado en `main.py`:

```python
# context = scenario_builders.create()
```

### Disrupciones Configuradas

Las disrupciones estan definidas en `scenario_builders/disruption_scenario.py`.

Tramos congestionados:

- `New Jersey -> Cartagena`
- `Shanghai -> Kaohsiung`
- `Kaohsiung -> Busan`
- `Kaohsiung -> Los Angeles`

Puertos cerrados temporalmente:

- `Cartagena`
- `Kaohsiung`

Como hay un warm-up de 140 dias, los dias de disrupcion configurados de forma relativa se desplazan internamente por ese warm-up.

## Como Ejecutar

Recomendado desde PowerShell, en la raiz del proyecto.

### Crear Y Activar Entorno Virtual

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Si `python` no esta disponible en el PATH, usar el Python que corresponda a la instalacion local.

### Instalar Dependencias

```powershell
pip install -r requirements.txt
```

El archivo `requirements.txt` incluye `-e ./o2despy`, por lo que instala la libreria local en modo editable.

### Correr La Simulacion

```powershell
python main.py
```

Al terminar, se escriben CSVs en `Output/` y se intenta abrir el dashboard local en:

```text
http://127.0.0.1:8000/dashboard/
```

### Lanzar Solo El Dashboard

```powershell
python dashboard/serve_gui.py
```

O sin abrir el navegador automaticamente:

```powershell
python dashboard/serve_gui.py --no-open
```

## Donde Se Implementa La Solucion

La solucion debe ir en `response_strategies/user_strategy.py`.

Ese archivo contiene cuatro puntos de decision:

1. `select_vessel_for_berth(...)`
   - Decide que buque recibe atraque cuando el puerto esta congestionado.

2. `create_alternative_service_routes(...)`
   - Permite crear rutas alternativas usando solo legs existentes.
   - No se pueden crear buques nuevos.
   - No se pueden crear legs nuevos.

3. `assign_associated_bookings(...)`
   - Asigna la cadena inicial de bookings para un shipment nuevo.

4. `adjust_bookings_before_cargo_handling(...)`
   - Replanifica shipments ya embarcados antes de que el buque haga carga/descarga en puerto.

Si una funcion devuelve `None`, el modelo usa la implementacion de fallback en `response_strategies/default_strategy.py`.

## Ideas De Mejora

Estas son opciones razonables para explorar sin tocar el motor:

- Ruteo preventivo para evitar Kaohsiung y Cartagena antes o durante disrupciones.
- Replanificacion en transito cuando el booking futuro pasa por un puerto cerrado o tramo congestionado.
- Asignacion de bookings por tiempo esperado, no solo por distancia.
- Penalizar transbordos si aumentan espera y riesgo.
- Priorizar buques con mas TEU descargable o carga mas antigua en puertos congestionados.
- Priorizar shipments grandes, ya que el KPI es ponderado por TEU.
- Crear rutas alternativas solo cuando realmente mejoren frente a esperar recuperacion.

## Validacion Recomendada

Antes de cambiar estrategias:

1. Correr el escenario actual y guardar `Output/ATT_By_Statistics_Interval.csv`.
2. Implementar una estrategia dentro de `response_strategies/`.
3. Correr la simulacion otra vez con la misma semilla.
4. Comparar:
   - `AverageTransportTime`
   - `Port_Waiting_Statistics.csv`
   - `Service_Route_Utilization.csv`
   - `Average_Vessel_State_Counts.csv`

Para pruebas de la libreria local:

```powershell
python -m pytest o2despy/tests -q
```

## Archivos De Salida Esperados

Despues de una corrida completa, el dashboard espera encontrar en `Output/`:

- `ATT_By_Statistics_Interval.csv`
- `Average_Origin_Waiting_TEU_By_OD.csv`
- `Average_In_Transit_TEU_By_OD.csv`
- `Cumulative_Completed_TEU_By_OD.csv`
- `Port_Waiting_Statistics.csv`
- `Service_Route_Utilization.csv`
- `Average_Vessel_State_Counts.csv`

Tambien puede usar el baseline:

- `Baseline_ATT_By_Statistics_Interval.csv`

## Notas Para Trabajo Futuro

- Evitar modificar `simulation_model/`, `scenario_builders/`, `Input/` o `config/` para la entrega competitiva.
- Mantener cualquier explicacion o documento de estrategia dentro de `response_strategies/` si se va a entregar.
- Medir cada cambio contra el default, porque una regla intuitiva puede empeorar el flujo si mueve congestion de un puerto a otro.
- La estrategia por defecto ya evita disrupciones activas; la oportunidad principal esta en anticipar, priorizar y replanificar mejor.

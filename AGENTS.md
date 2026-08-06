# AGENTS.md

Guia para agentes que trabajen en este proyecto.

## Contexto Del Proyecto

Este es el codigo Python de la ronda 1 del WSC 2026 Simulation Challenge. El objetivo practico es mejorar el flujo de contenedores bajo disrupciones usando estrategias de respuesta.

La regla central del reto es:

```text
Todo cambio competitivo debe estar dentro de response_strategies/
```

No asumir que cambios fuera de esa carpeta seran considerados en la entrega.

## Reglas De Trabajo

1. No modificar archivos fuera de `response_strategies/` salvo que el usuario lo pida explicitamente.
2. Si se necesita documentar una estrategia para entrega, poner la documentacion dentro de `response_strategies/`.
3. No cambiar datos de `Input/` para mejorar resultados competitivos.
4. No cambiar `config/`, `simulation_model/`, `scenario_builders/` ni `maritime_data_context/` para una solucion de competencia.
5. Mantener el comportamiento reproducible. La semilla principal esta en `main.py`.
6. Antes de editar, leer los contratos de `response_strategies/user_strategy.py` y las validaciones de `response_strategies/strategy_validation.py`.
7. Si se toca `create_alternative_service_routes`, respetar estrictamente que no se pueden crear buques ni legs nuevos.

## Puntos De Entrada

Archivo principal:

```text
main.py
```

Archivo de solucion del concursante:

```text
response_strategies/user_strategy.py
```

Estrategia fallback:

```text
response_strategies/default_strategy.py
```

Validaciones:

```text
response_strategies/strategy_validation.py
```

Configuracion:

```text
config/simulation_config.py
```

Disrupciones:

```text
scenario_builders/disruption_scenario.py
```

## Funciones Que Puede Implementar La Solucion

### `select_vessel_for_berth(...)`

Decide que buque recibe atraque cuando hay congestion suficiente en un puerto.

Ideas seguras:

- Priorizar mayor TEU descargable.
- Priorizar mayor edad de espera.
- Priorizar buques que liberen carga hacia destinos criticos.
- Priorizar combinacion de carga a descargar, carga a cargar y tiempo esperando.

Debe devolver un buque que exista en `waiting_vessels`.

### `create_alternative_service_routes(...)`

Puede crear rutas alternativas y reservar buques existentes.

Restricciones:

- No crear `Leg` nuevos.
- No crear `Vessel` nuevos.
- Toda ruta nueva debe usar legs existentes en `context.legs`.
- Toda ruta nueva debe formar un ciclo conectado.
- Los buques asignados a rutas nuevas deben venir de rutas existentes.

Esta es la zona de mayor riesgo tecnico.

### `assign_associated_bookings(...)`

Asigna el booking inicial de un shipment nuevo.

Ideas:

- Evitar puertos cerrados o pronto a cerrar.
- Penalizar tramos congestionados.
- Penalizar demasiados transbordos.
- Usar costo esperado, no solo distancia.
- Favorecer rutas con capacidad disponible si se puede estimar.

Debe crear objetos `Booking`, agregarlos a `shipment.associated_bookings`, registrarlos en `service_route.associated_bookings` y definir `shipment.current_booking_index`.

### `adjust_bookings_before_cargo_handling(...)`

Replanifica carga que ya esta en un buque antes de que ocurra la carga/descarga.

Ideas:

- Si el booking futuro pasa por Kaohsiung o Cartagena durante disrupcion, recalcular desde el puerto actual.
- Si el tramo futuro tiene multiplicador alto, buscar alternativa.
- Evitar replanificar si el desvio es peor que esperar.

Esta funcion es potente, pero puede romper cadenas de booking si no se actualizan indices y referencias inversas correctamente.

## Flujo De Simulacion Relevante

1. `ShipmentGenerator` crea shipments segun demanda.
2. `ShipmentWaitingForLoadingAtOriginPort` asigna bookings iniciales.
3. `VesselBeingServed` carga shipments en buques segun ruta y segmento.
4. `VesselSailing` mueve buques por segmentos.
5. `VesselQueuingForBerth` y `BerthIdle` gestionan cola y atraque.
6. `BerthHandlingCargo` calcula duracion de carga/descarga.
7. `ShipmentBeingTransported` marca shipments en transito o completados.

## Disrupciones Conocidas

Tramos congestionados:

- `New Jersey -> Cartagena`
- `Shanghai -> Kaohsiung`
- `Kaohsiung -> Busan`
- `Kaohsiung -> Los Angeles`

Puertos cerrados:

- `Cartagena`
- `Kaohsiung`

La configuracion esta en `scenario_builders/disruption_scenario.py`.

## KPI Principal

El KPI mas importante observado es:

```text
AverageTransportTime
```

Archivo:

```text
Output/ATT_By_Statistics_Interval.csv
```

El calculo es ponderado por TEU. Por eso, retrasos en shipments grandes pesan mas que retrasos en shipments pequenos.

## Comandos Utiles

Instalar dependencias:

```powershell
pip install -r requirements.txt
```

Correr simulacion:

```powershell
python main.py
```

Correr pruebas de `o2despy`:

```powershell
python -m pytest o2despy/tests -q
```

Lanzar dashboard:

```powershell
python dashboard/serve_gui.py
```

## Como Comparar Estrategias

1. Guardar salida default.
2. Cambiar solo `response_strategies/user_strategy.py`.
3. Correr `python main.py`.
4. Comparar `Output/ATT_By_Statistics_Interval.csv`.
5. Revisar tambien:
   - `Port_Waiting_Statistics.csv`
   - `Service_Route_Utilization.csv`
   - `Average_Vessel_State_Counts.csv`

Una mejora real deberia bajar `AverageTransportTime` sin crear acumulaciones extremas de espera en puertos o rutas.

## Cuidado Con Estos Riesgos

- Devolver un buque que no este en `waiting_vessels` causa error.
- Crear una ruta alternativa sin ciclo conectado causa error.
- Crear legs o buques nuevos causa error.
- Olvidar remover bookings viejos de `service_route.associated_bookings` puede dejar referencias inconsistentes.
- Cambiar indices de booking sin actualizar `shipment.current_booking_index` puede romper carga/descarga.
- Replanificar demasiado puede crear transbordos innecesarios y empeorar el KPI.

## Estrategia Recomendada Para Agentes

Orden sugerido de implementacion:

1. Prioridad de atraque por TEU descargable, edad de espera y carga critica.
2. Booking inicial con penalizacion para Kaohsiung, Cartagena y tramos congestionados.
3. Rebooking en transito solo si el camino restante contiene una disrupcion activa o inminente.
4. Rutas alternativas solo despues de tener mediciones que demuestren que el default no basta.

Mantener cambios pequenos y medibles. Despues de cada cambio, correr simulacion y comparar el KPI.

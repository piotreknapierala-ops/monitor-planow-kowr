# Monitor planów postępowań KOWR

Monitor codziennie sprawdza oficjalną stronę KOWR z planami postępowań Centrali i Oddziałów Terenowych, pobiera najnowsze plany na bieżący rok i wyciąga pozycje dotyczące robót budowlanych.

## Zakres raportu

- jednostka KOWR,
- pozycja planu,
- przedmiot zamówienia,
- orientacyjna wartość netto,
- planowany termin wszczęcia,
- numer i wersja planu,
- informacja o dodaniu, zmianie lub rezygnacji,
- bezpośredni link do PDF-u,
- próba powiązania pozycji planu z ogłoszeniem na platformie eB2B.

Jedna pozycja planu może zostać powiązana z kilkoma późniejszymi ogłoszeniami.

## Źródła

- https://www.gov.pl/web/kowr/plan-postepowan
- https://kowr.eb2b.com.pl/open-auctions.html

## Uruchamianie

Workflow działa automatycznie codziennie oraz ręcznie przez:

`Actions → Monitor planów KOWR → Run workflow`

GitHub Pages publikuje zawartość folderu `docs`.

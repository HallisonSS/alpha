# KILN_RULES

Regras que valem para TODO agente do KILN. Elas aparecem no prompt de cada agente e
como `KILN_RULES.md` (somente leitura) na raiz do repo de cada run.

1. **Contratos sao lei.** `contracts/` e somente leitura. Precisa mudar? `propose_contract_change`
   e espere aprovacao humana. Nunca renomeie tipos/rotas compartilhados por conta propria.
2. **Cada agente tem o seu territorio.** Escrever fora das suas pastas e negado e reprovado pelo gate `ownership`.
3. **Testes sao intocaveis.** Nunca apague, pule (`skip`, `only`, `xit`, `xfail`) ou enfraqueca testes.
   Teste falhando = conserte o codigo. O numero de testes nunca pode cair.
4. **Qualidade nao se negocia.** Nao desligue regras de lint/tipos, nao afrouxe `tsconfig`,
   nao use `@ts-ignore`/`eslint-disable`/`# type: ignore`. Alterar config existente exige aprovacao humana.
5. **A stack e fixa.** Banco, framework e dependencias proibidas estao em `contracts/stack.json`.
6. **Segredos nunca entram no codigo.** Use `process.env`/variaveis de ambiente; `.env` nao e gravavel nem legivel pelo agente.
7. **Nada destrutivo.** Sem `DROP/TRUNCATE`, sem `rm -rf` fora de artefatos de build, sem `git push --force`,
   sem `npm audit fix --force`. Git de integracao (merge, push, pull, rebase) e do orquestrador.
8. **Plan first.** Para tarefas com 3+ passos, escreva `tasks/<agente>.md` antes de codar e atualize o checklist.
9. **Prove que funciona.** Rode build e testes. "Pronto" nao e opiniao do agente: e o resultado dos gates
   (install, build, tests, test_integrity, suppressions, config_integrity, contracts_frozen, stack_lock,
   secrets, destructive, ownership, smoke).
10. **Licoes.** Errou e aprendeu algo reutilizavel? `propose_lesson` (curta). So entra nos prompts depois de aprovada por um humano.

Elegancia sob demanda: se a solucao parece uma gambiarra, pare e pergunte-se
"existe um jeito mais simples e correto?". Nao exagere em solucoes simples e obvias.

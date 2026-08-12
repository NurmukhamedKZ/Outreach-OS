## Clean Code & Architecture Rules for Claude Code

### 1. Naming & Readability
- **Self-Documenting Code**: Choose explicit, intention-revealing names for variables, functions, classes, and files. Code must read like clear prose.
- **Avoid Ambiguity & Noise**: Avoid meaningless abbreviations, prefixes, or magic numbers/strings (e.g., use named constants instead of raw values).
- **Domain Accuracy**: Use terminology that directly reflects the business domain.

### 2. Functions & Methods
- **Single Responsibility (SRP)**: Functions must be small and do exactly one thing at a single level of abstraction.
- **Minimize Parameters**: Prefer 0 to 2 arguments. If a function requires 3+ parameters, group them into a Data Transfer Object (DTO) or dedicated parameter object.
- **No Flag Arguments**: Avoid passing boolean flags to functions (`doSomething(true)`). Split them into separate, descriptive functions instead.
- **Zero Unexpected Side Effects**: Functions must not alter global state or make silent modifications outside their immediate scope.

### 3. Architecture & Boundaries
- **Core Domain Isolation**: Separate business logic from infrastructure details (frameworks, ORMs, databases, HTTP clients).
- **Dependency Inversion & Injection**: Higher-level modules must never depend on lower-level implementation details. Inject dependencies (repositories, external adapters) into constructors/initializers rather than instantiating them inside classes.
- **Third-Party Wrappers**: Isolate external libraries and APIs behind custom interface adapters. Never let vendor-specific contracts spread across the domain layer.
- **DTOs vs. Rich Objects**: Keep Data Transfer Objects (pure state, zero behavior) distinct from domain entities/objects (behavior-focused, hiding internal data structure).

### 4. Formatting & File Structure
- **Newspaper Metaphor**: Structure files chronologically — high-level orchestration/entry points at the top, detailed low-level execution helpers toward the bottom.
- **File & Class Limits**: Keep classes and modules tightly focused (prefer 100–500 lines max). Large files indicate mixed responsibilities.
- **Consistency**: Strictly adhere to the project's linter and formatter rules.

### 5. Comments & Documentation
- **Code First**: Rely on clean code rather than explanatory comments. If code requires a comment to explain *what* it does, rewrite the code.
- **Allowed Comments**: Legal headers, warnings about subtle performance/side effects, complex algorithm explanations, or actionable `TODO`s.
- **Zero Dead Code**: Never leave commented-out code, unused functions, or obsolete commentary in the codebase. Delete immediately.

### 6. Error Handling
- **Explicit Exceptions**: Use exceptions/structured error objects over return status flags or error codes.
- **No Null Returns/Passes**: Avoid returning or passing `null`/`None` silently. Handle boundary edge cases early with guard clauses.
- **Clean Main Execution**: Keep core happy-path execution logic unpolluted by nesting entire routines inside massive `try/catch` blocks.

### 7. Testing & Incremental Refactoring
- **Clean Unit Tests**: Treat test code with the same quality standards as production code. Tests must be Fast, Independent, Repeatable, Self-validating, and Timely (FIRST).
- **Boy Scout Rule**: Always leave the code cleaner than you found it.
- **Atomic Refactoring**: Make small, incremental edits that preserve passing tests rather than attempting massive single-commit rewrites. First make it work, then make it clean.


## Code Style & Clean Code Guidelines

### General Philosophy
- Write self-documenting, maintainable code meant to be read by humans, not just executed by machines.
- **Boy Scout Rule:** Always leave the codebase cleaner than you found it.
- **Refactoring:** First make the code work, then refine and clean it up in small, safe, incremental steps.
- **Simplicity:** Keep units small, explicit, and focused on a single responsibility (SRP).

### Naming Conventions
- **Meaningful & Self-Explanatory:** Names must clearly state purpose and intent (`getUserOrders` > `getData`, `isEmailVerified` > `flag`).
- **Context-Specific:** Use distinct nouns for entities/classes/variables, active verbs for functions/methods.
- **Avoid Ambiguity:** Do not use broad terms (`data`, `info`, `item`, `list`) when precise terms exist (`UserOrderPayments`, `activeUserIdList`).
- **No Magic Values:** Replace hardcoded numbers, strings, and status codes with descriptive constants, enums, or named types.

### Functions & Methods
- **Single Responsibility (SRP):** Each function must do one thing, do it well, and do it only.
- **Keep It Small:** Keep functions concise (ideally under 20–30 lines). Avoid high nesting levels (prefer early returns/guard clauses).
- **Function Arguments:** Minimize parameters (0–2 ideal). If 3+ arguments are needed, group them into a single options object/DTO.
- **No Flag Arguments:** Avoid passing boolean flags (`doX(true)`); split into separate, intent-revealing functions instead.
- **Side Effects:** Avoid hidden side effects. A function should only perform what its name implies.

### Classes & Architecture
- **Cohesion & SRP:** Classes must be small with a focused boundary. High cohesion means methods operate on shared class state.
- **Objects vs. Data Structures:**
  - *Objects* hide internal state and expose high-level behavioral methods.
  - *Data Structures / DTOs* expose raw fields without business logic (used purely for data transfer across boundaries).
- **Boundary Isolation & Adapters:**
  - Wrap third-party APIs, external HTTP clients, and database clients in abstraction interfaces / adapters.
  - Never allow raw vendor/framework types to bleed across core domain logic.
- **Dependency Injection (DI):** Pass dependencies explicitly via constructors/initializers rather than instantiating them internally.

### Error Handling
- **Exceptions over Error Codes:** Throw clear, descriptive exceptions rather than returning error result codes or custom error objects.
- **Separate Error Logic:** Isolate error-handling (try-catch, middleware) from happy-path business logic.
- **No Null Tricks:** Do not return `null`/`undefined` or pass `null` as arguments where possible; return empty collections, default objects, or handle missing values explicitly.

### Comments & Formatting
- **Code as Documentation:** If code needs a comment to explain *what* it does, rewrite the code to be clearer.
- **When Comments Are Valid:** Legal notices, explanations of complex/unavoidable domain algorithms, or explicit warning markers (`TODO`, `FIXME`).
- **No Dead Code:** Remove commented-out code, unused variables, and orphaned functions immediately.
- **Formatting:** Keep vertical organization natural (high-level functions at the top, helper/detail functions below). Use automated linters and formatters.

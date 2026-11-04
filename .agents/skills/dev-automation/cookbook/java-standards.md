# Java / Spring Boot Coding Standards

## Purpose
Reference document for the AI agent when writing or reviewing Java/Spring Boot code.
Apply these standards in all code changes and code reviews.

## Project Structure

```
src/main/java/com/company/project/
├── config/           # Spring configuration classes
├── controller/       # REST controllers (@RestController)
├── dto/              # Request/Response DTOs
│   ├── request/
│   └── response/
├── entity/           # JPA entities (@Entity)
├── enums/            # Enum types
├── exception/        # Custom exceptions and global handler
├── mapper/           # Object mappers (MapStruct or manual)
├── repository/       # Spring Data JPA repositories
├── service/          # Business logic (@Service)
│   └── impl/         # Service implementations
├── util/             # Utility classes
└── validation/       # Custom validators
```

## Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Class | PascalCase | `UserService`, `OrderController` |
| Method | camelCase, verb-first | `findByEmail()`, `createOrder()` |
| Variable | camelCase | `userName`, `orderList` |
| Constant | UPPER_SNAKE | `MAX_RETRY_COUNT`, `DEFAULT_PAGE_SIZE` |
| Package | lowercase | `com.company.project.service` |
| DTO | Suffix with Request/Response | `CreateUserRequest`, `UserResponse` |
| Entity | Singular noun | `User`, `Order`, `OrderItem` |
| Repository | Suffix with Repository | `UserRepository` |
| Service interface | Plain name | `UserService` |
| Service impl | Suffix with Impl | `UserServiceImpl` |
| Controller | Suffix with Controller | `UserController` |
| Exception | Suffix with Exception | `UserNotFoundException` |
| Test | Suffix with Test | `UserServiceTest` |

## Controller Layer

```java
@RestController
@RequestMapping("/api/v1/users")
@RequiredArgsConstructor
public class UserController {

    private final UserService userService;

    @GetMapping("/{id}")
    public ResponseEntity<UserResponse> getById(@PathVariable Long id) {
        return ResponseEntity.ok(userService.findById(id));
    }

    @PostMapping
    public ResponseEntity<UserResponse> create(
            @Valid @RequestBody CreateUserRequest request) {
        UserResponse created = userService.create(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(created);
    }
}
```

Rules:
- Use `@RequiredArgsConstructor` (Lombok) for constructor injection
- Return `ResponseEntity<T>` with appropriate HTTP status
- Use `@Valid` for request body validation
- Keep controllers thin — no business logic, just delegation

## Service Layer

```java
public interface UserService {
    UserResponse findById(Long id);
    UserResponse create(CreateUserRequest request);
}

@Service
@RequiredArgsConstructor
@Slf4j
public class UserServiceImpl implements UserService {

    private final UserRepository userRepository;
    private final UserMapper userMapper;

    @Override
    @Transactional(readOnly = true)
    public UserResponse findById(Long id) {
        User user = userRepository.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("User", id));
        return userMapper.toResponse(user);
    }

    @Override
    @Transactional
    public UserResponse create(CreateUserRequest request) {
        if (userRepository.existsByEmail(request.getEmail())) {
            throw new DuplicateResourceException("User", "email", request.getEmail());
        }
        User user = userMapper.toEntity(request);
        User saved = userRepository.save(user);
        return userMapper.toResponse(saved);
    }
}
```

Rules:
- Define interface + implementation
- Use `@Transactional(readOnly = true)` for read operations
- Use `@Transactional` for write operations
- Throw custom exceptions, not generic ones
- Log at appropriate levels (debug for flow, warn for recoverable issues, error for failures)

## Entity Layer

```java
@Entity
@Table(name = "users")
@Getter
@Setter
@NoArgsConstructor
@AllArgsConstructor
@Builder
public class User {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false, length = 100)
    private String name;

    @Column(nullable = false, unique = true)
    private String email;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private UserStatus status;

    @CreationTimestamp
    private LocalDateTime createdAt;

    @UpdateTimestamp
    private LocalDateTime updatedAt;
}
```

Rules:
- Use Lombok annotations for boilerplate reduction
- Define column constraints (`nullable`, `length`, `unique`)
- Use `@Enumerated(EnumType.STRING)` for enums (never ORDINAL)
- Use `LocalDateTime` for timestamps, not `Date`
- Never expose entities directly in API responses — use DTOs

## Exception Handling

```java
@RestControllerAdvice
@Slf4j
public class GlobalExceptionHandler {

    @ExceptionHandler(ResourceNotFoundException.class)
    public ResponseEntity<ErrorResponse> handleNotFound(ResourceNotFoundException ex) {
        log.warn("Resource not found: {}", ex.getMessage());
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(new ErrorResponse("NOT_FOUND", ex.getMessage()));
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ErrorResponse> handleValidation(MethodArgumentNotValidException ex) {
        String message = ex.getBindingResult().getFieldErrors().stream()
                .map(e -> e.getField() + ": " + e.getDefaultMessage())
                .collect(Collectors.joining(", "));
        return ResponseEntity.badRequest()
                .body(new ErrorResponse("VALIDATION_ERROR", message));
    }
}
```

## Testing Standards

```java
@ExtendWith(MockitoExtension.class)
class UserServiceImplTest {

    @Mock
    private UserRepository userRepository;
    @Mock
    private UserMapper userMapper;
    @InjectMocks
    private UserServiceImpl userService;

    @Test
    void should_returnUser_when_existsById() {
        // Arrange
        User user = User.builder().id(1L).name("Test").build();
        UserResponse expected = new UserResponse(1L, "Test");
        when(userRepository.findById(1L)).thenReturn(Optional.of(user));
        when(userMapper.toResponse(user)).thenReturn(expected);

        // Act
        UserResponse result = userService.findById(1L);

        // Assert
        assertThat(result).isEqualTo(expected);
        verify(userRepository).findById(1L);
    }

    @Test
    void should_throwNotFound_when_userDoesNotExist() {
        when(userRepository.findById(1L)).thenReturn(Optional.empty());

        assertThatThrownBy(() -> userService.findById(1L))
                .isInstanceOf(ResourceNotFoundException.class);
    }
}
```

Rules:
- Test name pattern: `should_<expected>_when_<condition>`
- Use Arrange-Act-Assert structure
- Use AssertJ for fluent assertions (`assertThat`)
- Mock only direct dependencies
- Test both happy path and error cases

## Common Anti-Patterns to Flag in Review

| Anti-Pattern | Correct Approach |
|---|---|
| Returning entity from controller | Use DTOs (Request/Response) |
| Catching generic `Exception` | Catch specific exceptions |
| Business logic in controller | Move to service layer |
| N+1 query in loop | Use `@EntityGraph` or `JOIN FETCH` |
| String concatenation for SQL | Use parameterized queries or JPQL |
| `new Service()` inside other classes | Use Spring DI (`@RequiredArgsConstructor`) |
| Hardcoded config values | Use `@Value` or `@ConfigurationProperties` |
| Missing `@Transactional` | Add to service methods that modify data |
| `System.out.println` | Use SLF4J logger (`@Slf4j`) |
| Mutable static fields | Use Spring beans or constants |

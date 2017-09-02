---
layout: post
title: Mockito and Powermockito Cheatsheet
comments: true
lang: en
thumbnail: hackergotchi_big.png
tags: [java,ut,mockito]
---

## Introduction
**mockito**: mock an object by interception and injection so that programmers don't need to write their own mock class.

**powermockito**: can mock constructor, static method, etc, by modifying bytecode.

## Code Examples
Kafka streams metrics exporter: https://pastebin.com/4UXW8M22

S3debug snapshot uploader: https://pastebin.com/aanJT2ph

## Basic
### Create Mock Object

```
@Mock FooClass fooInstance; // use annotation
MockitoAnnotations.initMocks(this); // command to create all mock instances
```

or 

```
FooClass fooInstance = Mockito.mock(FooClass.class) // programatically
```

##### Gotcha
@Mock annotated object can not be instantiated as initMocks creates a mocked object


### Intercept

Mockito.doNothing()
Mockito.doThrow()
Mockito.doAnswer()
Mockito.doReturn()

or 

```
PowerMockito.when(mockObject.mockedMethod()).thenReturn(value) // When the mockedMethod is called, return the value

PowerMockito.doNothing().when(mockObject).mockedMethod() // Do nothing when mockedMethod is called 
```

### Assertions

## Spy

```
@Test
public void whenNotUseSpyAnnotation_thenCorrect() {
    List<String> spyList = Mockito.spy(new ArrayList<String>());
     
    spyList.add("one");
    spyList.add("two");
 
    Mockito.verify(spyList).add("one");
    Mockito.verify(spyList).add("two");
 
    assertEquals(2, spyList.size());
 
    Mockito.doReturn(100).when(spyList).size();
    assertEquals(100, spyList.size());
}
```
or

```
@Spy
List<String> spiedList = new ArrayList<String>();
 
@Test
public void whenUseSpyAnnotation_thenSpyIsInjected() {
    spiedList.add("one");
    spiedList.add("two");
 
    Mockito.verify(spiedList).add("one");
    Mockito.verify(spiedList).add("two");
 
    assertEquals(2, spiedList.size());
 
    Mockito.doReturn(100).when(spiedList).size();
    assertEquals(100, spiedList.size());
}

```

Mock vs Spy: When Mockito creates a mock – it does so from the Class of an Type, not from an actual instance. The mock simply creates a bare-bones shell instance of the Class, entirely instrumented to track interactions with it.

On the other hand, the spy will wrap an existing instance. It will still behave in the same way as the normal instance – the only difference is that it will also be instrumented to track all the interactions with it.

Why use Spy?
when you already have an instance (e.g., you have to call specific ctor)

## Powermock
### Basic

```
@RunWith(PowerMockRunner.class)
@PrepareForTest({FooClass.class})

```

## Q&A

### How to mock a static method?
http://www.michaelminella.com/testing/how-to-mock-static-methods.html

### How to mock a 

#### Gotcha
##### use PowerMockito.when, not Mockito.when

##### ERROR: Following methods *cannot* be stubbed/verified: final/private/equals()/hashCode().

In order to use PowerMockito to stub/verify, need to have annotations

```
@RunWith(PowerMockRunner.class)
@PrepareForTest({FooClass.class})

```

### Simulate first call fails, second call succeeds
Chaining: 

```
when(mock.someMethod("some arg"))
   .thenThrow(new RuntimeException()).thenReturn("foo");
```

### Argument passed to when() is not a mock!
org.mockito.exceptions.misusing.NotAMockException: Argument passed to when() is not a mock!

### doAnswer() vs. thenAnswer()
no diff. However when you want to stub a method returning void you need to use 'doAnswer'. 





## References
1. [https://raseshmori.wordpress.com/2015/01/07/mockito-and-power-mockito-cheatsheet/]()
2. 


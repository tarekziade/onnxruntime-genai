/*
 * Copyright (c) Microsoft Corporation. All rights reserved.
 * Licensed under the MIT License.
 */
package ai.onnxruntime.genai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Arrays;
import java.util.function.Consumer;
import java.util.logging.Logger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;

// Test the overall generation.
// Uses SimpleGenAI with phi-2 (if available) for text -> text generation.
// Uses the HF test model with pre-defined input tokens for token -> token generation
//
// This indirectly tests the majority of the bindings. Any gaps are covered in the class specific
// tests.
public class GenerationTest {
  private static final Logger logger = Logger.getLogger(GenerationTest.class.getName());

  // phi-2 can be used in full end-to-end testing but needs to be manually downloaded.
  // it's also used this way in the C# unit tests.
  private static final String phi2ModelPath() {
    return TestUtils.getFilePathFromResource("/phi-2/int4/cpu");
  }

  @SuppressWarnings("unused") // Used in EnabledIf
  private static boolean havePhi2() {
    return phi2ModelPath() != null;
  }

  @SuppressWarnings("unused") // Used in EnabledIf
  private static boolean haveAdapters() {
    return TestUtils.testAdapterTestModelPath() != null;
  }

  @Test
  @EnabledIf("havePhi2")
  public void testUsageNoListener() throws GenAIException {
    try (SimpleGenAI generator = new SimpleGenAI(phi2ModelPath());
        GeneratorParams params =
            generator.createGeneratorParams(
                TestUtils.applyPhi2ChatTemplate("What's 6 times 7?")); ) {
      params.setSearchOption("max_length", 20);

      String result = generator.generate(params, null);
      logger.info("Result: " + result);
      assertNotNull(result);
    }
  }

  @Test
  @EnabledIf("havePhi2")
  public void testUsageWithListener() throws GenAIException {
    try (SimpleGenAI generator = new SimpleGenAI(phi2ModelPath());
        GeneratorParams params =
            generator.createGeneratorParams(
                TestUtils.applyPhi2ChatTemplate("What's 6 times 7?")); ) {
      params.setSearchOption("max_length", 20);

      Consumer<String> listener = token -> logger.info("onTokenGenerate: " + token);
      String result = generator.generate(params, listener);

      logger.info("Result: " + result);
      assertNotNull(result);
    }
  }

  @Test
  @EnabledIf("haveAdapters")
  public void testUsageWithAdapters() throws GenAIException {
    try (Model model = new Model(TestUtils.testAdapterTestModelPath());
        Tokenizer tokenizer = model.createTokenizer()) {
      String[] prompts = {
        TestUtils.applyPhi2ChatTemplate("def is_prime(n):"),
        TestUtils.applyPhi2ChatTemplate("def compute_gcd(x, y):"),
        TestUtils.applyPhi2ChatTemplate("def binary_search(arr, x):"),
      };

      try (Sequences sequences = tokenizer.encodeBatch(prompts);
          GeneratorParams params = model.createGeneratorParams()) {
        params.setSearchOption("max_length", 200);
        params.setInput(sequences);

        long[] outputShape;

        try (Generator generator = new Generator(model, params); ) {
          while (!generator.isDone()) {
            generator.computeLogits();
            generator.generateNextToken();
          }

          try (Tensor logits = generator.getOutput("logits")) {
            outputShape = logits.getShape();
            assertEquals(logits.getType(), Tensor.ElementType.float32);
          }
        }

        try (Adapters adapters = new Adapters(model);
            Generator generator = new Generator(model, params); ) {
          adapters.loadAdapters(TestUtils.testAdapterTestAdaptersPath(), "adapters_a_and_b");
          generator.setActiveAdapter(adapters, "adapters_a_and_b");
          while (!generator.isDone()) {
            generator.computeLogits();
            generator.generateNextToken();
          }
          try (Tensor logits = generator.getOutput("logits")) {
            assertEquals(logits.getType(), Tensor.ElementType.float32);
            assertTrue(Arrays.equals(outputShape, logits.getShape()));
          }
        }
      }
    }
  }

  @Test
  public void testWithInputIds() throws GenAIException {
    // test using the HF model. input id values must be < 1000 so we use manually created input.
    // Input/expected output copied from the C# unit tests
    try (Model model = new Model(TestUtils.testModelPath());
        GeneratorParams params = new GeneratorParams(model); ) {
      int batchSize = 2;
      int sequenceLength = 4;
      int maxLength = 10;
      int[] inputIDs =
          new int[] {
            0, 0, 0, 52,
            0, 0, 195, 731
          };

      params.setInput(inputIDs, sequenceLength, batchSize);
      params.setSearchOption("max_length", maxLength);

      int[] expectedOutput =
          new int[] {
            0, 0, 0, 52, 204, 204, 204, 204, 204, 204,
            0, 0, 195, 731, 731, 114, 114, 114, 114, 114
          };

      try (Sequences output = model.generate(params)) {
        assertEquals(output.numSequences(), batchSize);

        for (int i = 0; i < batchSize; i++) {
          int[] outputIds = output.getSequence(i);
          for (int j = 0; j < maxLength; j++) {
            assertEquals(outputIds[j], expectedOutput[i * maxLength + j]);
          }
        }
      }
    }
  }
}
